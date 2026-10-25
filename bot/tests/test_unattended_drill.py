"""The portable unattended runner: the same procedure as the GitHub workflow, as
one tested program for a host that can actually reach Bybit (GitHub's own
runners are blocked: HTTP 403 from CloudFront)."""
from __future__ import annotations

import json
import os
import signal
import sys
import time
import urllib.error

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))

import next_settlement as NS  # noqa: E402
import unattended_drill as U  # noqa: E402


def ms(h, mi=0, s=0, d=25):
    import datetime as dt
    return int(dt.datetime(2026, 10, d, h, mi, s, tzinfo=dt.timezone.utc).timestamp() * 1000)


INSIDE = ms(15, 50)           # 10 min before the 16:00 settlement
OUTSIDE = ms(11, 0)
ENV = {"BYBIT_API_KEY": "k", "BYBIT_API_SECRET": "s", "PATH": "/usr/bin",
       "LIVE_TRADING_ACK": "yes", "BYBIT_VENUE": "mainnet", "PAPER_TRADING": "1"}


class Recorder:
    """A fake `run_child`: records every call and answers per tool name."""

    def __init__(self, codes=None, make=()):
        self.calls = []
        self.codes = codes or {}
        self.make = make

    def __call__(self, argv, env, *, log_path, timeout_s=None):
        tool = os.path.basename(argv[1])
        self.calls.append((tool, list(argv), dict(env), timeout_s))
        code = self.codes.get(tool, 0)
        if isinstance(code, Exception):
            raise code
        if tool == "evidence_reconcile.py":
            report = argv[argv.index("--json") + 1]
            if code in (0, 2):
                open(report, "w").write(json.dumps({"schema": "evidence_reconciliation/1"}))
        return code

    def tools(self):
        return [c[0] for c in self.calls]


def run(tmp_path, rec, *, now=INSIDE, probe=200):
    return U.run_once(str(tmp_path / "r"), env=ENV, now_ms=now,
                      prober=lambda: probe, runner=rec)


class TestProbe:
    class Resp:
        def __init__(self, status):
            self.status = status

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def test_200(self):
        assert U.probe(lambda url, timeout: self.Resp(200)) == 200

    def test_the_cloudfront_block_is_its_status(self):
        def blocked(url, timeout):
            raise urllib.error.HTTPError(url, 403, "blocked", {}, None)
        assert U.probe(blocked) == 403

    def test_no_answer_is_zero(self):
        def down(url, timeout):
            raise urllib.error.URLError("no route")
        assert U.probe(down) == 0

    def test_it_only_ever_asks_the_public_time_endpoint(self):
        assert U.PROBE_URL == "https://api-testnet.bybit.com/v5/market/time"


class TestOneRun:
    def test_an_unreachable_venue_stops_before_anything_is_run(self, tmp_path):
        rec = Recorder()
        for status in (403, 0, 500):
            assert run(tmp_path, rec, probe=status) == U.EXIT_UNREACHABLE
        assert rec.calls == []

    def test_outside_a_window_sends_nothing(self, tmp_path):
        rec = Recorder()
        assert run(tmp_path, rec, now=OUTSIDE) == U.EXIT_OUTSIDE
        assert rec.calls == []

    def test_a_clean_run_goes_in_order_and_is_ok(self, tmp_path):
        rec = Recorder({"drill.py": 0})
        rec.codes = {}
        assert run(tmp_path, rec) == U.EXIT_OK
        assert rec.tools() == ["drill.py", "drill.py", "close_testnet_short.py",
                               "evidence_reconcile.py", "recon_summary.py"]
        verdict = json.load(open(tmp_path / "r" / "verdict.json"))
        assert verdict["ok"] is True

    def test_only_the_second_drill_call_is_armed_and_it_holds_across_one_settlement(self, tmp_path):
        rec = Recorder()
        run(tmp_path, rec)
        pre, armed = rec.calls[0][1], rec.calls[1][1]
        assert "--arm" not in pre
        for flag in ("--arm", "--hold-for-settlement", "--min-settlements"):
            assert flag in armed
        assert armed[armed.index("--max-hold-hours") + 1] == "0.5"
        assert armed[armed.index("--settle-wait-hours") + 1] == "0"
        assert rec.calls[1][3] == U.DRILL_TIMEOUT_S

    def test_the_safety_net_is_reduce_only_capped_and_unattended(self, tmp_path):
        rec = Recorder()
        run(tmp_path, rec)
        argv = rec.calls[2][1]
        assert argv[argv.index("--max-qty") + 1] == "0.0015"
        assert "--arm" in argv and "--no-prompt" in argv

    def test_every_child_sees_the_fixed_testnet_environment(self, tmp_path):
        rec = Recorder()
        run(tmp_path, rec)
        for _tool, _argv, env, _t in rec.calls:
            assert env["BYBIT_VENUE"] == "testnet" and env["USE_TESTNET"] == "1"
            assert env["PAPER_TRADING"] == "0" and env["BOOK_MODE"] == "carry"
            assert env["CARRY_EXECUTION_MODE"] == "overlay"
            assert "LIVE_TRADING_ACK" not in env
            assert env["STATE_DB_PATH"].startswith(str(tmp_path / "r"))
            assert not os.path.exists(env["STATE_DB_PATH"])
            assert env["BYBIT_API_KEY"] == "k"

    def test_the_callers_environment_is_not_modified(self, tmp_path):
        before = dict(ENV)
        run(tmp_path, Recorder())
        assert ENV == before

    def test_a_failed_preflight_runs_no_drill_but_still_closes(self, tmp_path):
        rec = Recorder({"drill.py": 1})
        assert run(tmp_path, rec) == U.EXIT_FAILED
        assert rec.tools() == ["drill.py", "close_testnet_short.py"]

    @pytest.mark.parametrize("drill_exc", [OSError("cannot start"), RuntimeError("x")])
    def test_a_drill_that_cannot_start_still_gets_the_safety_net(self, tmp_path, drill_exc):
        calls = {"n": 0}

        class R(Recorder):
            def __call__(self, argv, env, **kw):
                if os.path.basename(argv[1]) == "drill.py" and "--arm" in argv:
                    raise drill_exc
                return super().__call__(argv, env, **kw)
        rec = R()
        assert run(tmp_path, rec) == U.EXIT_FAILED
        assert "close_testnet_short.py" in rec.tools()

    def test_the_safety_net_runs_even_if_this_process_is_interrupted(self, tmp_path):
        class R(Recorder):
            def __call__(self, argv, env, **kw):
                if os.path.basename(argv[1]) == "drill.py" and "--arm" in argv:
                    raise KeyboardInterrupt
                return super().__call__(argv, env, **kw)
        rec = R()
        with pytest.raises(KeyboardInterrupt):
            run(tmp_path, rec)
        assert "close_testnet_short.py" in rec.tools()

    def test_a_missing_reconciliation_report_is_a_failure(self, tmp_path):
        rec = Recorder({"evidence_reconcile.py": 3})
        assert run(tmp_path, rec) == U.EXIT_FAILED
        assert json.load(open(tmp_path / "r" / "verdict.json"))["reconcile"] == 99

    def test_the_log_says_why_it_did_nothing(self, tmp_path, capsys):
        run(tmp_path, Recorder(), probe=403)
        assert "NOT reachable" in capsys.readouterr().out


class TestVerdict:
    @pytest.mark.parametrize("kw,ok", [
        (dict(preflight=0, drill=5, flat=0, recon=2), True),
        (dict(preflight=0, drill=0, flat=0, recon=0), True),
        (dict(preflight=0, drill=5, flat=1, recon=2), False),
        (dict(preflight=0, drill=5, flat=None, recon=2), False),
        (dict(preflight=1, drill=None, flat=0, recon=None), False),
        (dict(preflight=0, drill=130, flat=0, recon=2), False),
        (dict(preflight=0, drill=124, flat=0, recon=2), False),
        (dict(preflight=0, drill=None, flat=0, recon=None), False),
        (dict(preflight=0, drill=5, flat=0, recon=3), False),
        (dict(preflight=0, drill=5, flat=0, recon=99), False),
        (dict(preflight=0, drill=5, flat=0, recon=None), False),
    ])
    def test_table(self, kw, ok):
        assert U.verdict(**kw)["ok"] is ok

    def test_a_not_ok_verdict_says_why(self):
        assert U.verdict(preflight=0, drill=5, flat=1, recon=2)["problems"]


class TestRunChild:
    def test_exit_code_and_output_are_captured(self, tmp_path):
        log = str(tmp_path / "l")
        code = U.run_child([sys.executable, "-c", "print('hi'); raise SystemExit(7)"],
                           os.environ, log_path=log)
        assert code == 7 and b"hi" in open(log, "rb").read()

    def test_a_timeout_sends_sigterm_first_so_the_drill_can_unwind(self, tmp_path, monkeypatch):
        marker = tmp_path / "got_term"
        child = (
            "import signal,sys,time\n"
            f"def h(s,f):\n open({str(marker)!r},'w').write('term'); sys.exit(0)\n"
            "signal.signal(signal.SIGTERM,h)\n"
            "print('ready',flush=True)\n"
            "time.sleep(60)\n")
        monkeypatch.setattr(U, "TERM_GRACE_S", 5)
        code = U.run_child([sys.executable, "-c", child], os.environ,
                           log_path=str(tmp_path / "l"), timeout_s=1.0)
        assert code == 124 and marker.read_text() == "term"

    def test_a_child_that_ignores_sigterm_is_killed(self, tmp_path, monkeypatch):
        child = ("import signal,time\n"
                 "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                 "time.sleep(60)\n")
        monkeypatch.setattr(U, "TERM_GRACE_S", 1)
        t0 = time.time()
        code = U.run_child([sys.executable, "-c", child], os.environ,
                           log_path=str(tmp_path / "l"), timeout_s=1.0)
        assert code == 124 and time.time() - t0 < 15


class TestTheLoop:
    class Clock:
        def __init__(self, start_ms):
            self.t = start_ms / 1000.0
            self.sleeps = []

        def now(self):
            return self.t

        def sleep(self, s):
            self.sleeps.append(s)
            self.t += s

    def test_next_start(self):
        assert U.next_start_ms(ms(11, 0)) == ms(15, 40)
        assert U.next_start_ms(ms(15, 45)) == ms(15, 45)          # window is open
        assert U.next_start_ms(ms(15, 58)) == ms(23, 40)          # too late for this one
        assert U.next_start_ms(ms(23, 59)) == ms(7, 40, d=26)

    def test_it_sleeps_until_the_window_runs_once_and_does_not_repeat_the_window(self, tmp_path):
        clock = self.Clock(ms(11, 0))
        runs = []

        def once(run_dir, env):
            runs.append((run_dir, clock.now()))
            return 0
        code = U.loop(str(tmp_path), env=ENV, max_runs=1, clock=clock.now,
                      sleep=clock.sleep, once=once)
        assert code == 0 and len(runs) == 1
        start = runs[0][1]
        assert ms(15, 40) / 1000 <= start < ms(16, 0) / 1000

    def test_two_runs_are_two_different_windows(self, tmp_path):
        clock = self.Clock(ms(11, 0))
        runs = []

        def once(run_dir, env):
            runs.append(clock.now())
            return 0
        U.loop(str(tmp_path), env=ENV, max_runs=2, clock=clock.now,
               sleep=clock.sleep, once=once)
        assert len(runs) == 2
        assert runs[0] < ms(16, 0) / 1000 <= ms(23, 40) / 1000 <= runs[1] < ms(0, 0, d=26) / 1000

    def test_the_stop_file_ends_it_before_a_run(self, tmp_path):
        (tmp_path / "STOP").write_text("")
        clock = self.Clock(ms(15, 50))
        runs = []
        code = U.loop(str(tmp_path), env=ENV, clock=clock.now, sleep=clock.sleep,
                      once=lambda *a, **k: runs.append(1) or 0)
        assert code == 0 and runs == []

    def test_the_stop_file_ends_it_while_sleeping(self, tmp_path):
        clock = self.Clock(ms(11, 0))
        runs = []

        def sleeper(s):
            clock.sleep(s)
            (tmp_path / "STOP").write_text("")
        code = U.loop(str(tmp_path), env=ENV, clock=clock.now, sleep=sleeper,
                      once=lambda *a, **k: runs.append(1) or 0)
        assert code == 0 and runs == []

    def test_a_second_loop_on_the_same_root_is_refused(self, tmp_path):
        first = U.acquire_lock(str(tmp_path))
        assert first is not None
        assert U.acquire_lock(str(tmp_path)) is None
        first.close()
        again = U.acquire_lock(str(tmp_path))
        assert again is not None
        again.close()


class TestMain:
    def test_missing_credentials_are_a_refusal_that_names_the_variable_only(self, monkeypatch, capsys):
        monkeypatch.delenv("BYBIT_API_SECRET", raising=False)
        monkeypatch.setenv("BYBIT_API_KEY", "sekrit-value")
        assert U.main(["--run-dir", "/tmp/x"]) == U.EXIT_REFUSED
        err = capsys.readouterr().err
        assert "BYBIT_API_SECRET" in err and "sekrit-value" not in err

    def test_a_negative_max_runs_is_refused(self, monkeypatch):
        monkeypatch.setenv("BYBIT_API_KEY", "k")
        monkeypatch.setenv("BYBIT_API_SECRET", "s")
        assert U.main(["--run-dir", "/tmp/x", "--loop", "--max-runs", "-1"]) == 3

    def test_the_credentials_never_appear_in_the_orchestrators_own_output(self, tmp_path, capsys):
        env = dict(ENV, BYBIT_API_KEY="KEYVALUE123", BYBIT_API_SECRET="SECRETVALUE456")
        U.run_once(str(tmp_path / "r"), env=env, now_ms=INSIDE,
                   prober=lambda: 200, runner=Recorder())
        text = capsys.readouterr().out + open(tmp_path / "r" / "orchestrator.log").read()
        assert "KEYVALUE123" not in text and "SECRETVALUE456" not in text
