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
import run_outcomes as RO  # noqa: E402
import unattended_drill as U  # noqa: E402

sys.path.insert(0, HERE)
import runner_fixtures as RF  # noqa: E402


def ms(h, mi=0, s=0, d=25):
    import datetime as dt
    return int(dt.datetime(2026, 10, d, h, mi, s, tzinfo=dt.timezone.utc).timestamp() * 1000)


INSIDE = ms(15, 50)           # 10 min before the 16:00 settlement
OUTSIDE = ms(11, 0)
ENV = {"BYBIT_API_KEY": "k", "BYBIT_API_SECRET": "s", "PATH": "/usr/bin",
       "LIVE_TRADING_ACK": "yes", "BYBIT_VENUE": "mainnet", "PAPER_TRADING": "1"}


class Recorder:
    """A fake `run_child`: records every call and answers per tool name.

    `elig` is what the (read-only) carry_preflight.py reports about the entry:
    PASS | REFUSED | UNAVAILABLE | None (no readable report)."""

    def __init__(self, codes=None, make=(), elig="PASS", recon=None):
        self.calls = []
        self.codes = codes or {}
        self.make = make
        self.elig = elig
        self.recon = RF.reconciliation() if recon is None else recon

    def __call__(self, argv, env, *, log_path, timeout_s=None):
        tool = os.path.basename(argv[1])
        self.calls.append((tool, list(argv), dict(env), timeout_s))
        code = self.codes.get(tool, 0)
        if isinstance(code, Exception):
            raise code
        if tool == "carry_preflight.py" and self.elig:
            with open(log_path, "w") as fh:
                fh.write(json.dumps(RF.preflight_report(self.elig, order_gate_open=True)))
        if tool == "evidence_reconcile.py":
            report = argv[argv.index("--json") + 1]
            if code in (0, 2):
                open(report, "w").write(json.dumps(self.recon))
        return code

    def tools(self):
        return [c[0] for c in self.calls]


def run(tmp_path, rec, *, now=INSIDE, probe=200):
    return U.run_once(str(tmp_path / "r"), env=ENV, now_ms=now,
                      prober=lambda: probe, runner=rec, sleep=lambda s: None)


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
        assert rec.tools() == ["drill.py", "carry_preflight.py", "drill.py",
                               "close_testnet_short.py", "evidence_reconcile.py",
                               "recon_summary.py"]
        assert rec.calls[0][3] == U.PREFLIGHT_TIMEOUT_S      # the preflight cannot hang forever
        verdict = json.load(open(tmp_path / "r" / "verdict.json"))
        assert verdict["ok"] is True

    def test_only_the_second_drill_call_is_armed_and_it_holds_across_one_settlement(self, tmp_path):
        rec = Recorder()
        run(tmp_path, rec)
        pre, armed = rec.calls[0][1], rec.calls[2][1]
        assert "--arm" not in pre
        for flag in ("--arm", "--hold-for-settlement", "--min-settlements"):
            assert flag in armed
        assert armed[armed.index("--max-hold-hours") + 1] == "0.5"
        assert armed[armed.index("--settle-wait-hours") + 1] == "0"
        assert rec.calls[2][3] == U.DRILL_TIMEOUT_S

    def test_the_safety_net_is_reduce_only_capped_and_unattended(self, tmp_path):
        rec = Recorder()
        run(tmp_path, rec)
        argv = rec.calls[3][1]
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
        # a usable reconciliation report is no longer enough: the CAPTURED CYCLE
        # must be VENUE_RECONCILED (`reconciled=True`) for a conformance pass
        (dict(preflight=0, drill=5, flat=0, recon=2, reconciled=True), True),
        (dict(preflight=0, drill=0, flat=0, recon=0, reconciled=True), True),
        (dict(preflight=0, drill=5, flat=0, recon=2, reconciled=False), False),
        (dict(preflight=0, drill=5, flat=0, recon=2, reconciled=None), False),
        (dict(preflight=0, drill=5, flat=0, recon=2), False),
        # a strategy refusal is not an operational error, if the venue is flat
        (dict(preflight=0, drill=None, flat=0, recon=None,
              not_eligible="BREAKEVEN_HOLD_EXCEEDS_DEMONSTRATED_HOLD"), True),
        (dict(preflight=0, drill=None, flat=1, recon=None,
              not_eligible="BREAKEVEN_HOLD_EXCEEDS_DEMONSTRATED_HOLD"), False),
        (dict(preflight=0, drill=None, flat=0, recon=None,
              assessment_failed=True), False),
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
        assert U.main(["--conformance", "--run-dir", "/tmp/x"]) == U.EXIT_REFUSED
        err = capsys.readouterr().err
        assert "BYBIT_API_SECRET" in err and "sekrit-value" not in err

    def test_a_negative_max_runs_is_refused(self, monkeypatch):
        monkeypatch.setenv("BYBIT_API_KEY", "k")
        monkeypatch.setenv("BYBIT_API_SECRET", "s")
        assert U.main(["--conformance", "--run-dir", "/tmp/x", "--loop", "--max-runs", "-1"]) == 3

    def test_the_credentials_never_appear_in_the_orchestrators_own_output(self, tmp_path, capsys):
        env = dict(ENV, BYBIT_API_KEY="KEYVALUE123", BYBIT_API_SECRET="SECRETVALUE456")
        U.run_once(str(tmp_path / "r"), env=env, now_ms=INSIDE,
                   prober=lambda: 200, runner=Recorder(), sleep=lambda s: None)
        text = capsys.readouterr().out + open(tmp_path / "r" / "orchestrator.log").read()
        assert "KEYVALUE123" not in text and "SECRETVALUE456" not in text


# ---------------------------------------------------------------------------
# found by independent review
# ---------------------------------------------------------------------------

import subprocess  # noqa: E402
import textwrap  # noqa: E402


class TestTheReportIsNotWrittenBesideItsEvidence:
    def paths(self, tmp_path):
        rec = Recorder()
        run(tmp_path, rec)
        argv = next(c[1] for c in rec.calls if c[0] == "evidence_reconcile.py")
        return argv[argv.index("--evidence") + 1], argv[argv.index("--json") + 1]

    def test_the_directories_differ(self, tmp_path):
        ev, rep = self.paths(tmp_path)
        assert os.path.dirname(ev) != os.path.dirname(rep)

    def test_the_real_reconcile_tool_accepts_this_layout(self, tmp_path, capsys):
        import evidence_reconcile as R
        ev, rep = self.paths(tmp_path)
        os.makedirs(os.path.dirname(ev), exist_ok=True)
        open(ev, "w").write("")
        drill = tmp_path / "r" / "drill_out.json"
        drill.write_text("{}")
        R.main(["--evidence", ev, "--drill-report", str(drill),
                "--now-ms", "1", "--json", rep])
        assert "beside the evidence" not in capsys.readouterr().err

    def test_the_safety_nets_evidence_is_in_the_evidence_folder_too(self, tmp_path):
        rec = Recorder()
        run(tmp_path, rec)
        close = next(c[1] for c in rec.calls if c[0] == "close_testnet_short.py")
        path = close[close.index("--evidence-path") + 1]
        assert os.path.basename(os.path.dirname(path)) == "evidence"


class TestTheWindowIsCheckedAgainBeforeTheArmedDrill:
    def test_a_slow_preflight_that_closes_the_window_means_no_order(self, tmp_path, monkeypatch):
        ticks = iter([INSIDE / 1000.0])
        later = (INSIDE + 11 * 60_000) / 1000.0     # 16:01, settlement passed

        def clock():
            return next(ticks, later)
        monkeypatch.setattr(U.time, "time", clock)
        rec = Recorder()
        code = U.run_once(str(tmp_path / "r"), env=ENV, prober=lambda: 200,
                          runner=rec, sleep=lambda s: None)
        assert rec.tools() == ["drill.py", "carry_preflight.py",
                               "close_testnet_short.py"]
        assert code == U.EXIT_FAILED
        assert "window closed" in "".join(
            json.load(open(tmp_path / "r" / "verdict.json"))["problems"])


class TestTheProbeRetries:
    def test_a_blip_then_ok_runs(self, tmp_path):
        answers = iter([403, 200])
        sleeps = []
        code = U.run_once(str(tmp_path / "r"), env=ENV, now_ms=INSIDE,
                          prober=lambda: next(answers), runner=Recorder(),
                          sleep=sleeps.append)
        assert code == 0 and sleeps == [U.PROBE_PAUSE_S]

    def test_a_persistent_block_stops_after_three_tries(self, tmp_path):
        calls, sleeps = [], []
        code = U.run_once(str(tmp_path / "r"), env=ENV, now_ms=INSIDE,
                          prober=lambda: calls.append(1) or 403,
                          runner=Recorder(), sleep=sleeps.append)
        assert code == U.EXIT_UNREACHABLE and len(calls) == U.PROBE_TRIES
        assert len(sleeps) == U.PROBE_TRIES - 1


class TestHousekeeping:
    def test_prune_keeps_the_newest_and_touches_nothing_else(self, tmp_path):
        names = [f"run-2026102{d}T154000Z" for d in range(1, 8)]
        for n in names:
            (tmp_path / n).mkdir()
        (tmp_path / "keepme").mkdir()
        (tmp_path / "run-notadate").mkdir()
        (tmp_path / "run-20261025T154000Z.txt").write_text("x")
        target = tmp_path / "elsewhere"
        target.mkdir()
        (tmp_path / "run-20261020T154000Z").symlink_to(target)
        gone = U.prune_runs(str(tmp_path), keep=3)
        assert gone == names[:4]
        left = sorted(os.listdir(tmp_path))
        assert names[4:] == [n for n in left if n in names]
        assert {"keepme", "run-notadate", "elsewhere"} <= set(left)
        assert (tmp_path / "run-20261025T154000Z.txt").exists()
        assert target.exists()

    def test_a_refused_second_loop_does_not_wipe_the_holders_pid(self, tmp_path):
        first = U.acquire_lock(str(tmp_path))
        assert U.acquire_lock(str(tmp_path)) is None
        assert (tmp_path / "loop.lock").read_text() == str(os.getpid())
        first.close()

    def test_stop_is_honoured_while_stepping_past_a_window(self, tmp_path):
        clock = TestTheLoop.Clock(ms(15, 50))

        def sleeper(s):
            clock.sleep(s)
            (tmp_path / "STOP").write_text("")
        runs = []
        code = U.loop(str(tmp_path), env=ENV, clock=clock.now, sleep=sleeper,
                      once=lambda *a, **k: runs.append(1) or 4)
        assert code == 0 and len(runs) == 1 and len(clock.sleeps) <= 2

    def test_a_shutdown_inside_a_run_ends_the_loop_with_143(self, tmp_path):
        clock = TestTheLoop.Clock(ms(15, 50))

        def once(*a, **k):
            raise U.Shutdown("signal 15")
        assert U.loop(str(tmp_path), env=ENV, clock=clock.now, sleep=clock.sleep,
                      once=once) == U.EXIT_SIGNALLED

    def test_main_turns_a_shutdown_into_143(self, monkeypatch, tmp_path):
        monkeypatch.setenv("BYBIT_API_KEY", "k")
        monkeypatch.setenv("BYBIT_API_SECRET", "s")
        monkeypatch.setattr(U, "install_signal_handlers", lambda: None)

        def boom(*a, **k):
            raise U.Shutdown("signal 15")
        monkeypatch.setattr(U, "run_once", boom)
        assert U.main(["--conformance", "--run-dir", str(tmp_path)]) == U.EXIT_SIGNALLED


STUB = textwrap.dedent("""
    import os, signal, sys, time
    PREFLIGHT_PASS = @@PF@@
    D = os.environ["STUB_DIR"]
    name = os.path.basename(sys.argv[0])
    armed = "--arm" in sys.argv
    def mark(n, text=""):
        open(os.path.join(D, n), "w").write(text)
    if name == "drill.py" and armed:
        mark("drill_pid", str(os.getpid()))
        def term(s, f):
            mark("drill_got_term")
            time.sleep(1.0)               # the unwind
            mark("drill_unwound")
            sys.exit(130)
        signal.signal(signal.SIGTERM, term)
        mark("drill_armed")
        time.sleep(120)
        sys.exit(0)
    if name == "close_testnet_short.py":
        mark("close_started", "drill_unwound=%d" % os.path.exists(os.path.join(D, "drill_unwound")))
        time.sleep(float(os.environ.get("STUB_CLOSE_SECONDS", "0")))
        mark("close_done")
        sys.exit(0)
    if name == "carry_preflight.py":
        print(PREFLIGHT_PASS)
    if name == "evidence_reconcile.py":
        open(sys.argv[sys.argv.index("--json") + 1], "w").write("{}")
    sys.exit(0)
""")

STUB_SRC = STUB.replace("@@PF@@", repr(json.dumps(
    RF.preflight_report("PASS", order_gate_open=True))))

DRIVER = textwrap.dedent("""
    import os, sys
    sys.path.insert(0, {tools!r})
    import unattended_drill as U
    U.TOOLS = {stubs!r}
    U.TERM_GRACE_S = 20
    U.install_signal_handlers()
    try:
        code = U.run_once({run!r}, env=os.environ, now_ms={now},
                          prober=lambda: 200, sleep=lambda s: None)
    except U.Shutdown:
        code = U.EXIT_SIGNALLED
    sys.exit(code)
""")


class TestRealSignalsToARealOrchestrator:
    """The reviewer's reproductions: a signal to the ORCHESTRATOR must stop the
    drill (not orphan it) and only then run the safety net."""

    def start(self, tmp_path, close_seconds="0"):
        stubs = tmp_path / "stubs"
        stubs.mkdir()
        for n in ("drill.py", "close_testnet_short.py", "evidence_reconcile.py",
                  "recon_summary.py", "carry_preflight.py"):
            (stubs / n).write_text(STUB_SRC)
        marks = tmp_path / "marks"
        marks.mkdir()
        driver = tmp_path / "driver.py"
        driver.write_text(DRIVER.format(
            tools=os.path.join(os.path.dirname(HERE), "tools"), stubs=str(stubs),
            run=str(tmp_path / "run"), now=INSIDE))
        env = dict(os.environ, STUB_DIR=str(marks), BYBIT_API_KEY="k",
                   BYBIT_API_SECRET="s", STUB_CLOSE_SECONDS=close_seconds)
        proc = subprocess.Popen([sys.executable, str(driver)], env=env)
        for _ in range(100):
            if (marks / "drill_armed").exists():
                return proc, marks
            time.sleep(0.1)
        proc.kill()
        pytest.fail("the stub drill never armed")

    @staticmethod
    def alive(pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True

    @pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGHUP, signal.SIGINT])
    def test_the_drill_is_stopped_and_unwound_before_the_safety_net_runs(
            self, tmp_path, sig):
        proc, marks = self.start(tmp_path)
        drill_pid = int((marks / "drill_pid").read_text())
        proc.send_signal(sig)
        assert proc.wait(timeout=60) == U.EXIT_SIGNALLED
        assert (marks / "drill_got_term").exists()
        assert (marks / "drill_unwound").exists()
        assert (marks / "close_started").read_text() == "drill_unwound=1"
        assert (marks / "close_done").exists()
        assert not self.alive(drill_pid)                  # no orphan

    def test_a_second_signal_while_the_safety_net_runs_does_not_interrupt_it(
            self, tmp_path):
        proc, marks = self.start(tmp_path, close_seconds="3")
        proc.send_signal(signal.SIGTERM)
        for _ in range(100):
            if (marks / "close_started").exists():
                break
            time.sleep(0.1)
        proc.send_signal(signal.SIGTERM)
        proc.send_signal(signal.SIGINT)
        assert proc.wait(timeout=60) == U.EXIT_SIGNALLED
        assert (marks / "close_done").exists()

    def test_a_signal_during_the_safety_net_is_acted_on_after_it(self, tmp_path):
        """No drill running (the stub drill exits at once when the armed branch
        is skipped by a failing preflight is not used here): the signal lands
        while the close runs; the close finishes, then the shutdown follows."""
        stubs = tmp_path / "stubs"
        stubs.mkdir()
        for n in ("drill.py", "close_testnet_short.py", "evidence_reconcile.py",
                  "recon_summary.py", "carry_preflight.py"):
            (stubs / n).write_text(STUB_SRC.replace("time.sleep(120)", "sys.exit(0)"))
        marks = tmp_path / "marks"
        marks.mkdir()
        driver = tmp_path / "driver.py"
        driver.write_text(DRIVER.format(
            tools=os.path.join(os.path.dirname(HERE), "tools"), stubs=str(stubs),
            run=str(tmp_path / "run"), now=INSIDE))
        env = dict(os.environ, STUB_DIR=str(marks), BYBIT_API_KEY="k",
                   BYBIT_API_SECRET="s", STUB_CLOSE_SECONDS="3")
        proc = subprocess.Popen([sys.executable, str(driver)], env=env)
        for _ in range(100):
            if (marks / "close_started").exists():
                break
            time.sleep(0.1)
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=60) == U.EXIT_SIGNALLED
        assert (marks / "close_done").exists()


def _verdict_file(tmp_path):
    return json.load(open(tmp_path / "r" / "verdict.json"))


class TestWhatTheRunLearned:
    """Every run ends in an outcome that says what it learned. A no-trade run is
    never a conformance pass, and a correct refusal is never an error."""

    def test_an_eligible_run_that_reconciles_is_a_conformance_pass(self, tmp_path):
        assert run(tmp_path, Recorder()) == U.EXIT_OK
        v = _verdict_file(tmp_path)
        assert v["outcome"] == RO.CONFORMANCE_PASSED
        assert v["conformance_passed"] is True and v["ok"] is True

    def test_an_ineligible_entry_arms_nothing_sends_nothing_and_is_not_a_pass(
            self, tmp_path):
        rec = Recorder(elig="REFUSED")
        assert run(tmp_path, rec) == U.EXIT_NOT_ELIGIBLE
        # the unarmed preflight, the read-only assessment, the flat check: no
        # armed drill, no reconciliation
        assert rec.tools() == ["drill.py", "carry_preflight.py",
                               "close_testnet_short.py"]
        assert not any("--arm" in c[1] for c in rec.calls
                       if c[0] == "drill.py")
        v = _verdict_file(tmp_path)
        assert v["outcome"] == RO.ENTRY_NOT_ELIGIBLE and v["ok"] is True
        assert v["conformance_passed"] is False
        assert v["refusal_reason"] == "BREAKEVEN_HOLD_EXCEEDS_DEMONSTRATED_HOLD"
        assert U.EXIT_NOT_ELIGIBLE not in (U.EXIT_OK,)

    @pytest.mark.parametrize("elig", ["UNAVAILABLE", None])
    def test_an_unassessable_entry_is_a_failure_not_a_refusal(self, tmp_path, elig):
        rec = Recorder(elig=elig)
        assert run(tmp_path, rec) == U.EXIT_FAILED
        assert not any("--arm" in c[1] for c in rec.calls if c[0] == "drill.py")
        v = _verdict_file(tmp_path)
        assert v["outcome"] == RO.CONFORMANCE_FAILED and v["ok"] is False

    def test_a_cycle_that_is_not_venue_reconciled_is_not_a_pass(self, tmp_path):
        rec = Recorder(recon=RF.reconciliation("LOCAL_RECORD", resolved=False))
        assert run(tmp_path, rec) == U.EXIT_FAILED
        v = _verdict_file(tmp_path)
        assert v["outcome"] == RO.CONFORMANCE_FAILED
        assert "VENUE_RECONCILED" in " ".join(v["problems"])

    def test_the_engine_standing_aside_at_the_open_is_a_refusal_not_a_failure(
            self, tmp_path):
        """Funding moved between the assessment and the open: the armed drill's
        own engine stood aside and sent nothing (drill exit 2, orders_sent 0)."""
        class R(Recorder):
            def __call__(self, argv, env, **kw):
                if os.path.basename(argv[1]) == "drill.py" and "--arm" in argv:
                    json.dump({"verdict": "FAILED", "orders_sent": 0, "stages": [
                        {"stage": "open", "ok": False, "why": "the book did not open",
                         "evidence": {"action": "stand_aside",
                                      "reason": "BREAKEVEN_HOLD_EXCEEDS_DEMONSTRATED_HOLD"}}]},
                        open(argv[argv.index("--out") + 1], "w"))
                return super().__call__(argv, env, **kw)
        class Armed2(R):
            def __call__(self, argv, env, **kw):
                code = super().__call__(argv, env, **kw)
                if os.path.basename(argv[1]) == "drill.py" and "--arm" in argv:
                    return 2
                return code
        rec = Armed2()
        assert run(tmp_path, rec) == U.EXIT_NOT_ELIGIBLE
        assert "evidence_reconcile.py" not in rec.tools()
        v = _verdict_file(tmp_path)
        assert v["outcome"] == RO.ENTRY_NOT_ELIGIBLE and v["ok"] is True

    def test_a_drill_failure_with_orders_sent_is_still_a_failure(self, tmp_path):
        class R(Recorder):
            def __call__(self, argv, env, **kw):
                code = super().__call__(argv, env, **kw)
                if os.path.basename(argv[1]) == "drill.py" and "--arm" in argv:
                    json.dump({"verdict": "FAILED", "orders_sent": 2, "stages": [
                        {"stage": "open", "ok": False, "evidence": {
                            "action": "stand_aside", "reason": "X"}}]},
                        open(argv[argv.index("--out") + 1], "w"))
                    return 1
                return code
        assert run(tmp_path, R()) == U.EXIT_FAILED
        assert _verdict_file(tmp_path)["outcome"] == RO.CONFORMANCE_FAILED

    def test_the_orchestrator_refuses_to_run_without_conformance(self, monkeypatch,
                                                                 capsys):
        monkeypatch.setenv("BYBIT_API_KEY", "k")
        monkeypatch.setenv("BYBIT_API_SECRET", "s")
        called = []
        monkeypatch.setattr(U, "run_once", lambda *a, **k: called.append(1) or 0)
        assert U.main(["--run-dir", "/tmp/x"]) == U.EXIT_REFUSED
        assert U.main(["--run-dir", "/tmp/x", "--loop"]) == U.EXIT_REFUSED
        assert called == []
        assert "runner_monitor" in capsys.readouterr().err

    def test_the_assessment_child_is_the_existing_read_only_preflight(self, tmp_path):
        rec = Recorder()
        run(tmp_path, rec)
        argv = [c[1] for c in rec.calls if c[0] == "carry_preflight.py"][0]
        assert argv[1].endswith(os.path.join("tools", "carry_preflight.py"))
        assert len(argv) == 2                      # no flag that could arm anything
