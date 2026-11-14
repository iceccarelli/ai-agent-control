"""Slack alerts for the deployed monitor.

The webhook URL is a secret. It is read only from SLACK_WEBHOOK_URL, and nothing
here may print, log, return or raise it. The URLs in this file are SYNTHETIC
(assembled from parts, pointing at no workspace); they exist only so redaction
can be tested.

The notifier must be unable to change a monitor outcome, exit code or order
gate: every failure inside it is swallowed (Exception only) and reported in the
monitor's own log, and a stop signal (a BaseException) still passes through.
"""
from __future__ import annotations

import calendar
import json
import os
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(BOT, "tools"))
sys.path.insert(0, BOT)

import runner_alerts as RA  # noqa: E402
import runner_monitor as RM  # noqa: E402
import unattended_drill as UD  # noqa: E402

TOKEN = "notarealtoken" + "0123456789"
FAKE_URL = "https://hooks.slack.com/services/" + "T0FAKE/B0FAKE/" + TOKEN
ENV = {RA.ENV_VAR: FAKE_URL}
NOW = float(calendar.timegm((2026, 10, 26, 12, 0, 0)))


def stamp(minutes_ago):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - minutes_ago * 60))


def row(minutes_ago, outcome="ENTRY_NOT_ELIGIBLE", **kw):
    r = {"at": stamp(minutes_ago), "outcome": outcome, "order_sent": False,
         "reasons": [], "shadow_state": "STEP_RECORDED"}
    r.update(kw)
    return r


def text(*rows):
    return "".join(json.dumps(r) + "\n" for r in rows)


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    monkeypatch.setattr(RA, "RETRY_BACKOFF_S", (0.0, 0.0))


class Wire:
    """An injected transport that records what would have been posted."""

    def __init__(self, status=200, boom=None):
        self.sent, self.status, self.boom = [], status, boom

    def __call__(self, url, body, timeout):
        if self.boom is not None:
            raise self.boom
        self.sent.append((url, json.loads(body.decode("utf-8")), timeout))
        return self.status


class Log:
    def __init__(self):
        self.lines = []

    def __call__(self, line):
        self.lines.append(str(line))

    def joined(self):
        return "\n".join(self.lines)


# -- reading the webhook ------------------------------------------------------

class TestTheWebhookComesFromTheEnvironmentOnly:
    def test_a_valid_url_enables_the_notifier(self):
        n = RA.Notifier(ENV, transport=Wire())
        assert n.enabled and n.disabled_reason is None

    @pytest.mark.parametrize("env", [{}, {RA.ENV_VAR: ""}, {RA.ENV_VAR: "   "}])
    def test_an_absent_url_disables_it_and_says_why(self, env):
        n = RA.Notifier(env, transport=Wire())
        assert not n.enabled
        assert RA.ENV_VAR in n.disabled_reason and "not set" in n.disabled_reason

    @pytest.mark.parametrize("bad", [
        "http://hooks.slack.com/services/T/B/x",            # not https
        "https://evil.example.com/services/T/B/x",          # not slack
        "https://hooks.slack.com.evil.example/services/T/B/x",
        "https://hooks.slack.com/other/T/B/x",
        "https://user:pw@hooks.slack.com/services/T/B/x",
        "file:///etc/passwd",
    ])
    def test_a_url_that_is_not_a_slack_webhook_is_refused_and_never_echoed(self, bad):
        n = RA.Notifier({RA.ENV_VAR: bad}, transport=Wire())
        assert not n.enabled
        assert bad not in n.disabled_reason and "hooks.slack.com" in n.disabled_reason

    @pytest.mark.parametrize("bad", [
        "https://hooks.slack.com/services/T/B/x?x=1",
        "https://hooks.slack.com/services/T/B/x#frag",
        "https://hooks.slack.com/services/../../T/B/x",
        "https://hooks.slack.com/services/%2e%2e/T/B/x",
        "https://hooks.slack.com/services//T/B/x",
        "https://hooks.slack.com/services/T/B/x y",
        "https://hooks.slack.com/services/T/B/x@evil.com",
        "https://hooks.slack.com/services/T/B",
    ])
    def test_the_path_must_be_exactly_three_plain_segments(self, bad):
        assert not RA.valid_webhook(bad)
        assert not RA.Notifier({RA.ENV_VAR: bad}, transport=Wire()).enabled

    def test_the_shape_of_a_real_slack_webhook_is_accepted(self):
        assert RA.valid_webhook(FAKE_URL)

    def test_the_url_is_never_in_the_repr_or_str_of_the_notifier(self):
        n = RA.Notifier(ENV, transport=Wire())
        assert TOKEN not in repr(n) and TOKEN not in str(n)


# -- sending and redaction ----------------------------------------------------

class TestSending:
    def test_a_message_is_posted_as_json_text_with_a_short_timeout(self):
        wire = Wire()
        n = RA.Notifier(ENV, transport=wire)
        assert n.send(RA.Alert("k", RA.WARNING, "hello")) == "sent"
        url, body, timeout = wire.sent[0]
        assert url == FAKE_URL and "hello" in body["text"] and timeout <= 10

    def test_a_disabled_notifier_sends_nothing_and_says_so(self):
        wire = Wire()
        n = RA.Notifier({}, transport=wire)
        assert n.send(RA.Alert("k", RA.CRITICAL, "x")) == "disabled"
        assert wire.sent == []

    def test_the_posted_text_never_carries_the_webhook_even_if_the_alert_does(self):
        wire = Wire()
        n = RA.Notifier(ENV, transport=wire)
        n.send(RA.Alert("k", RA.WARNING, f"history said {FAKE_URL} and {TOKEN}"))
        body = json.dumps(wire.sent[0][1])
        assert TOKEN not in body and FAKE_URL not in body and "T0FAKE" not in body

    def test_slack_control_syntax_in_history_text_cannot_ping_or_link(self):
        wire = Wire()
        n = RA.Notifier(ENV, transport=wire)
        n.send(RA.Alert("k", RA.WARNING,
                        "x <!channel> <!here> <@U123> <https://evil.example|click> & y"))
        text = wire.sent[0][1]["text"]
        assert "<" not in text and ">" not in text
        assert "&lt;!channel&gt;" in text and "&amp;" in text

    def test_text_is_truncated(self):
        wire = Wire()
        RA.Notifier(ENV, transport=wire).send(RA.Alert("k", RA.WARNING, "x" * 10000))
        assert len(wire.sent[0][1]["text"]) <= RA.MAX_TEXT + 200

    def test_a_failing_transport_is_swallowed_and_the_url_is_redacted(self):
        log = Log()
        boom = RuntimeError(f"connection to {FAKE_URL} refused (token {TOKEN})")
        n = RA.Notifier(ENV, transport=Wire(boom=boom), say=log)
        out = n.send(RA.Alert("k", RA.WARNING, "x"))
        assert out.startswith("failed")
        shown = out + log.joined()
        assert TOKEN not in shown and FAKE_URL not in shown
        assert "T0FAKE/B0FAKE" not in shown

    def test_a_non_200_answer_is_a_failure_not_a_success(self):
        n = RA.Notifier(ENV, transport=Wire(status=500), say=Log())
        assert n.send(RA.Alert("k", RA.WARNING, "x")).startswith("failed")

    def test_a_stop_signal_inside_the_transport_still_propagates(self):
        n = RA.Notifier(ENV, transport=Wire(boom=UD.Shutdown("SIGTERM")))
        with pytest.raises(UD.Shutdown):
            n.send(RA.Alert("k", RA.WARNING, "x"))

    def test_redact_removes_the_secret_the_token_and_any_slack_url(self):
        s = RA.redact(f"a {FAKE_URL} b {TOKEN} c https://hooks.slack.com/services/X/Y/Z", FAKE_URL)
        assert TOKEN not in s and "hooks.slack.com/services/X" not in s
        assert "[redacted]" in s

    def test_the_default_transport_refuses_a_non_slack_host(self):
        with pytest.raises(ValueError):
            RA.post("https://example.com/services/a/b/c", b"{}", 1.0)


# -- what is worth an alert ---------------------------------------------------

class TestEvaluate:
    def ev(self, *rows, **kw):
        return RA.evaluate(text(*rows), NOW, **kw)

    def test_a_healthy_history_alerts_nothing(self):
        assert self.ev(row(120), row(60), row(5)) == []

    def test_a_dead_runner_alerts_stale(self):
        a = self.ev(row(400), row(300))
        assert [x.key for x in a] == ["stale"] and a[0].severity == RA.CRITICAL

    def test_one_failed_cycle_is_not_a_streak(self):
        assert self.ev(row(120), row(5, "MONITOR_FAILED", reasons=["blip"])) == []

    def test_two_failed_cycles_in_a_row_alert(self):
        a = self.ev(row(180), row(120, "MONITOR_FAILED"), row(5, "MONITOR_FAILED",
                                                                reasons=["testnet down"]))
        assert [x.key for x in a] == ["failed_streak"]
        assert "testnet down" in a[0].text

    def test_a_failure_followed_by_success_is_no_streak(self):
        assert self.ev(row(120, "MONITOR_FAILED"), row(60, "MONITOR_FAILED"), row(5)) == []

    @pytest.mark.parametrize("state", ["FAULT", "REFUSED"])
    def test_a_shadow_fault_or_refusal_alerts(self, state):
        a = self.ev(row(60), row(5, shadow_state=state, shadow_detail="boom"))
        assert [x.key for x in a] == ["shadow"] and "boom" in a[0].text

    def test_order_sent_other_than_false_pages_critically(self):
        a = self.ev(row(60), row(5, order_sent=True))
        assert a[0].key == "order_sent" and a[0].severity == RA.CRITICAL

    def test_an_order_sent_claim_in_an_unreadable_line_pages(self):
        raw = text(row(60)) + '{"at": "x", "order_sent": true, "outc\n' + text(row(5))
        assert RA.evaluate(raw, NOW)[0].key == "order_sent"

    def test_a_history_with_no_readable_cycle_alerts(self):
        a = RA.evaluate("", NOW)
        assert [x.key for x in a] == ["no_data"]


class TestProblemsAreKeyedByWhatTheySay:
    def test_two_different_generic_problems_are_two_alerts(self):
        rows = [row(120), row(5, outcome="SOMETHING_ELSE")]
        future = row(-60 * 24 * 400)
        a = RA.evaluate(text(row(120), future), NOW)
        assert len({x.key for x in a if x.key.startswith("problem")}) >= 1
        both = RA.evaluate(text(row(60, outcome="SOMETHING_ELSE"), future), NOW)
        keys = [x.key for x in both if x.key.startswith("problem")]
        assert len(keys) == len(set(keys)) and len(keys) >= 2


# -- not nagging, and saying when it is over ----------------------------------

class TestThrottle:
    def test_the_same_alert_is_not_repeated_inside_the_window(self):
        t = RA.Throttle(resend_s=3600)
        a = RA.Alert("stale", RA.CRITICAL, "x")
        assert t.plan([a], 0.0) == [a]
        assert t.plan([a], 1800.0) == []
        assert t.plan([a], 3601.0) == [a]

    def test_order_sent_is_never_throttled(self):
        t = RA.Throttle(resend_s=3600)
        a = RA.Alert("order_sent", RA.CRITICAL, "x")
        assert t.plan([a], 0.0) == [a] and t.plan([a], 1.0) == [a]

    def test_a_cleared_condition_sends_one_recovery(self):
        t = RA.Throttle(resend_s=3600)
        a = RA.Alert("stale", RA.CRITICAL, "x")
        t.plan([a], 0.0)
        rec = t.plan([], 60.0)
        assert [r.key for r in rec] == ["stale:recovered"] and rec[0].severity == RA.RECOVERED
        assert t.plan([], 120.0) == []

    def test_two_different_conditions_are_independent(self):
        t = RA.Throttle(resend_s=3600)
        a, b = RA.Alert("stale", RA.CRITICAL, "x"), RA.Alert("shadow", RA.WARNING, "y")
        assert t.plan([a], 0.0) == [a]
        assert t.plan([a, b], 10.0) == [b]


# -- a hung cycle -------------------------------------------------------------

class TestTheWatchdogHasItsOwnThrottle:
    """One shared throttle treated each caller's list as the whole active set:
    the watchdog's [hung] made the main loop's `stale` look cleared."""

    def test_a_hung_alert_does_not_clear_a_stale_one(self, tmp_path):
        root = str(tmp_path)
        os.makedirs(root, exist_ok=True)
        a = RM._Alerts(root, RA.Notifier(ENV, transport=Wire(), say=Log()),
                       lambda: NOW)
        assert a.throttle is not a.watchdog_throttle
        stale = RA.Alert("stale", RA.CRITICAL, "x")
        assert a.throttle.plan([stale], 0.0) == [stale]
        hung = RA.Alert("hung", RA.CRITICAL, "y")
        assert a.watchdog_throttle.plan([hung], 1.0) == [hung]
        # the loop's throttle still knows `stale` is active and not yet due again
        assert a.throttle.plan([stale], 2.0) == []


class TestWatchdog:
    def test_a_cycle_that_never_finishes_is_reported(self):
        now = [0.0]
        w = RA.Watchdog(stale_minutes=150, clock=lambda: now[0])
        assert w.tick() is None
        now[0] = 150 * 60 - 1
        assert w.tick() is None
        now[0] = 150 * 60 + 1
        a = w.tick()
        assert a is not None and a.key == "hung" and a.severity == RA.CRITICAL

    def test_a_finished_cycle_resets_it(self):
        now = [0.0]
        w = RA.Watchdog(stale_minutes=150, clock=lambda: now[0])
        now[0] = 150 * 60 + 1
        w.beat()
        assert w.tick() is None


# -- wired into the loop, with no power over it -------------------------------

def _loop(tmp_path, notifier, outcomes, **kw):
    os.makedirs(str(tmp_path), exist_ok=True)
    root = str(tmp_path)
    seq = iter(outcomes)
    clock = {"t": NOW}

    def once(run_dir, *, env, history_root, **_):
        outcome = next(seq)
        os.makedirs(run_dir, exist_ok=True)
        with open(os.path.join(history_root, "monitor.jsonl"), "a") as fh:
            fh.write(json.dumps(row(0, outcome, reasons=["r"],
                                    at=time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                     time.gmtime(clock["t"])))) + "\n")
        clock["t"] += 3600
        return 0 if outcome != "MONITOR_FAILED" else RM.EXIT_FAILED

    code = RM.loop(root, env={}, interval_s=0.0, max_runs=len(outcomes),
                   clock=lambda: clock["t"], sleep=lambda s: None, once=once,
                   notifier=notifier, **kw)
    return code, root


class TestTheLoop:
    def test_a_failed_streak_sends_one_alert_and_the_exit_code_is_unchanged(self, tmp_path):
        wire = Wire()
        n = RA.Notifier(ENV, transport=wire, say=Log())
        code, _ = _loop(tmp_path / "with", n, ["MONITOR_FAILED", "MONITOR_FAILED"])
        baseline, _ = _loop(tmp_path / "without", None, ["MONITOR_FAILED", "MONITOR_FAILED"])
        assert code == baseline == RM.EXIT_FAILED
        # DELIBERATELY CHANGED: the loop now also sends one INFO startup message,
        # so the alert count excludes INFO
        pages = [m for m in wire.sent if "[INFO]" not in m[1]["text"]]
        assert len(pages) == 1 and "FAILED" in pages[0][1]["text"]

    def test_a_broken_notifier_cannot_change_the_outcome(self, tmp_path):
        n = RA.Notifier(ENV, transport=Wire(boom=RuntimeError(FAKE_URL)), say=Log())
        code, root = _loop(tmp_path, n, ["MONITOR_FAILED", "MONITOR_FAILED"])
        assert code == RM.EXIT_FAILED
        log = open(os.path.join(root, "orchestrator.log")).read() \
            if os.path.exists(os.path.join(root, "orchestrator.log")) else ""
        assert TOKEN not in log and FAKE_URL not in log

    def test_an_exception_in_the_alert_step_is_contained(self, tmp_path):
        class Exploding(RA.Notifier):
            def send(self, alert):
                raise RuntimeError("bug " + FAKE_URL)
        n = Exploding(ENV, transport=Wire(), say=Log())
        code, root = _loop(tmp_path, n, ["MONITOR_FAILED", "MONITOR_FAILED"])
        assert code == RM.EXIT_FAILED
        log = open(os.path.join(root, "orchestrator.log")).read()
        assert "alert step failed" in log and TOKEN not in log

    def test_a_missing_webhook_fails_loud_in_the_monitors_own_log(self, tmp_path):
        n = RA.Notifier({}, transport=Wire(), say=Log())
        _, root = _loop(tmp_path, n, ["ENTRY_NOT_ELIGIBLE", "ENTRY_NOT_ELIGIBLE"])
        log = open(os.path.join(root, "orchestrator.log")).read()
        assert log.count("ALERTS DISABLED") >= 1 and RA.ENV_VAR in log

    def test_no_notifier_at_all_also_says_alerts_are_off(self, tmp_path):
        _, root = _loop(tmp_path, None, ["ENTRY_NOT_ELIGIBLE"])
        assert "ALERTS DISABLED" in open(os.path.join(root, "orchestrator.log")).read()

    def test_a_healthy_run_sends_nothing(self, tmp_path):
        wire = Wire()
        n = RA.Notifier(ENV, transport=wire, say=Log())
        _loop(tmp_path, n, ["ENTRY_NOT_ELIGIBLE", "MONITOR_READY", "ENTRY_NOT_ELIGIBLE"])
        # DELIBERATELY CHANGED: the loop now sends one INFO startup message
        # ("alerts are ON"); a healthy run sends no WARNING or CRITICAL
        assert all("[INFO]" in m[1]["text"] for m in wire.sent)
        assert len(wire.sent) == 1


def test_an_interval_that_would_make_a_healthy_runner_look_dead_is_refused(
        tmp_path, monkeypatch, capsys):
    """`loop` is replaced, so if the guard were broken this would fail instead of
    starting a real monitor loop."""
    for name in UD.CREDENTIALS:
        monkeypatch.setenv(name, "x")
    started = []
    monkeypatch.setattr(RM, "loop", lambda *a, **k: started.append(k) or 0)
    for minutes in ("200", "inf", "nan", "0.5"):
        code = RM.main(["--loop", "--run-dir", str(tmp_path / "r"),
                        "--interval-minutes", minutes])
        assert code == RM.EXIT_REFUSED, minutes
    assert started == []
    assert "between 1 and 120" in capsys.readouterr().err
    assert RM.main(["--loop", "--run-dir", str(tmp_path / "r2"),
                    "--interval-minutes", "60"]) == 0
    assert len(started) == 1


def test_the_webhook_never_reaches_the_preflight_child():
    env = RM.monitor_env({RA.ENV_VAR: FAKE_URL, "BYBIT_API_KEY": "k"}, "/tmp/x")
    assert RA.ENV_VAR not in env and env["PAPER_TRADING"] == "1"


# -- the command line (for a watcher that is NOT on the runner) ---------------

class TestCommandLine:
    def run(self, tmp_path, rows, capsys, *args, env=None):
        path = tmp_path / "h.jsonl"
        path.write_text(text(*rows))
        code = RA.main([str(path), "--now", str(NOW), *args], env=env or {})
        return code, capsys.readouterr()

    def test_a_dry_run_prints_the_alerts_and_sends_nothing(self, tmp_path, capsys):
        code, out = self.run(tmp_path, [row(400)], capsys, "--dry-run", env=ENV)
        assert "stale" in out.out.lower() and TOKEN not in out.out + out.err
        assert code == 1

    def test_the_exit_code_mirrors_the_health_report(self, tmp_path, capsys):
        assert self.run(tmp_path, [row(5)], capsys)[0] == 0
        assert self.run(tmp_path, [row(5, order_sent=True)], capsys)[0] == 3

    def test_it_sends_through_the_notifier_and_never_prints_the_url(self, tmp_path, capsys, monkeypatch):
        wire = Wire()
        monkeypatch.setattr(RA, "post", wire)
        path = tmp_path / "h.jsonl"
        path.write_text(text(row(400)))
        code = RA.main([str(path), "--now", str(NOW)], env=ENV)
        shown = capsys.readouterr()
        assert code == 1 and len(wire.sent) == 1
        assert TOKEN not in shown.out + shown.err

    def test_a_missing_webhook_is_loud_on_stderr(self, tmp_path, capsys):
        code, out = self.run(tmp_path, [row(400)], capsys)
        assert RA.ENV_VAR in out.err and code == 1


    def test_a_failing_transport_is_reported_as_not_delivered_not_as_a_quiet_red(self, tmp_path, capsys):
        """REPRODUCED (Round 22): with Slack unreachable the watcher still exited 1 and said nothing distinct; a red job could not say whether anyone had been told."""
        def dead(url, body, timeout):
            raise OSError("network is unreachable " + FAKE_URL)
        n = RA.Notifier(ENV, transport=dead, say=lambda line: None, sleep=lambda s: None)
        path = tmp_path / "h.jsonl"
        path.write_text(text(row(400)))
        result = tmp_path / "result.json"
        summary = tmp_path / "summary.md"
        code = RA.main([str(path), "--now", str(NOW), "--result-file", str(result)], env={**ENV, "GITHUB_ACTIONS": "true", "GITHUB_STEP_SUMMARY": str(summary)}, notifier=n)
        shown = capsys.readouterr()
        assert code == 1                                                       # still red: an alert was raised
        assert "DELIVERY: none_delivered" in shown.out and "::error title=monitor alerts were raised but NOT delivered" in shown.out
        got = json.loads(result.read_text())
        assert got["delivery"] == "none_delivered" and got["raised"] and all(v.startswith("failed") for v in got["results"].values())
        assert "none_delivered" in summary.read_text() and "stale" in summary.read_text().lower()
        assert TOKEN not in shown.out + shown.err + result.read_text() + summary.read_text()

    def test_delivered_disabled_dry_run_and_quiet_are_each_named(self, tmp_path, capsys):
        wire = Wire()
        ok = RA.Notifier(ENV, transport=wire, say=lambda line: None, sleep=lambda s: None)
        path = tmp_path / "h.jsonl"
        path.write_text(text(row(400)))
        r = tmp_path / "r.json"
        assert RA.main([str(path), "--now", str(NOW), "--result-file", str(r)], env=ENV, notifier=ok) == 1
        assert json.loads(r.read_text())["delivery"] == "all_sent" and "::error" not in capsys.readouterr().out
        RA.main([str(path), "--now", str(NOW), "--result-file", str(r)], env={})
        assert json.loads(r.read_text())["delivery"] == "disabled"
        RA.main([str(path), "--now", str(NOW), "--dry-run", "--result-file", str(r)], env=ENV)
        assert json.loads(r.read_text())["delivery"] == "dry_run"
        path.write_text(text(row(5)))
        RA.main([str(path), "--now", str(NOW), "--result-file", str(r)], env=ENV)
        assert json.loads(r.read_text())["delivery"] == "none_needed" and json.loads(r.read_text())["raised"] == []

    def test_a_partial_delivery_is_partial(self):
        a, b = RA.Alert("x", RA.WARNING, "t"), RA.Alert("y", RA.WARNING, "t")
        assert RA.delivery_status([a, b], {"x": "sent", "y": "failed: HTTP 500"}) == "partial"
        assert RA.delivery_status([a, b], {"x": "failed: OSError", "y": "failed: OSError"}) == "none_delivered"
        assert RA.delivery_status([a], {"x": "disabled"}) == "disabled"


# -- it can read, it cannot trade ---------------------------------------------

def test_the_module_has_no_order_capable_import_and_no_credentials():
    src = open(os.path.join(BOT, "tools", "runner_alerts.py"), encoding="utf-8").read()
    for banned in ("carry_broker", "bybit_connection", "place_market", "requests",
                   "BYBIT_API", "LIVE_TRADING_ACK", "place_order"):
        assert banned not in src, banned


# -- the market recorder -----------------------------------------------------------------------------------

def _keys(alerts):
    return [a.key for a in alerts]


def _recorder_rows(*states):
    n = len(states)
    return [row((n - i) * 60, recorder_state=s, **({"recorder_detail": "OSError: disk is full"} if s == "FAULT" else {}))
            for i, s in enumerate(states)]


def test_three_cycles_in_a_row_without_a_recording_recorder_raise_recorder_down():
    alerts = RA.evaluate(text(*_recorder_rows("RECORDING", "ABSENT", "STALE", "FAULT")), NOW)
    assert "recorder_down" in _keys(alerts)
    msg = next(a for a in alerts if a.key == "recorder_down")
    assert msg.severity == RA.WARNING and "disk is full" in msg.text and "depth is not accumulating" in msg.text


def test_one_or_two_bad_cycles_or_a_recovered_recorder_do_not_alert():
    for states in (("RECORDING", "FAULT", "FAULT"), ("FAULT", "FAULT", "RECORDING"), ("ABSENT", "STALE")):
        alerts = RA.evaluate(text(*_recorder_rows(*states)), NOW)
        assert "recorder_down" not in _keys(alerts), states


def test_a_history_with_no_recorder_field_never_raises_recorder_down():
    alerts = RA.evaluate(text(row(180), row(120), row(60)), NOW)
    assert "recorder_down" not in _keys(alerts)


# -- the recorder's volume: early warning before the write reserve --------------------------------------------

def _free_rows(*mbs):
    """History lines as the monitor WRITES them: every recorder field is prefixed, so the free space is ``recorder_free_mb`` (an end-to-end test through the real writer is in test_recorder_liveness)."""
    n = len(mbs)
    return [row((n - i) * 60, recorder_state="RECORDING", recorder_free_mb=mb) for i, mb in enumerate(mbs)]


def test_a_volume_that_stays_within_one_day_of_the_write_reserve_warns_before_the_recorder_stops():
    alerts = RA.evaluate(text(*_free_rows(5000, 1400, 1300, 1200)), NOW)
    assert "recorder_disk_low" in _keys(alerts) and "recorder_down" not in _keys(alerts)          # still RECORDING: this is the notice, not the failure
    msg = next(a for a in alerts if a.key == "recorder_disk_low")
    assert msg.severity == RA.WARNING and "1200 MiB" in msg.text and "only if the volume holds the retained days plus the reserve" in msg.text and "fly volumes list" in msg.text


def test_a_briefly_low_or_comfortable_volume_does_not_warn():
    for mbs in ((5000, 4000, 3000, 2500), (1400, 1300, 3500), (1400, 1300), (3000, 1400, 1300, 3500)):    # comfortable / recovered by the retention prune / too few rows
        assert "recorder_disk_low" not in _keys(RA.evaluate(text(*_free_rows(*mbs)), NOW)), mbs


def test_below_the_reserve_the_warning_says_below():
    msg = next(a for a in RA.evaluate(text(*_free_rows(400, 300, 200)), NOW) if a.key == "recorder_disk_low")
    assert "BELOW the 512 MiB write reserve" in msg.text


def test_rows_without_a_numeric_free_space_never_raise_the_disk_warning():
    rows = [row(180, recorder_state="RECORDING", recorder_free_mb=None), row(120, recorder_state="RECORDING", recorder_free_mb="low"), row(60, recorder_state="RECORDING", recorder_free_mb=True)]
    assert "recorder_disk_low" not in _keys(RA.evaluate(text(*rows), NOW))
    # the UNPREFIXED key is not what the monitor writes: a row carrying it is not a reading (it was the key the first version of this alert wrongly read, so it was inert in production)
    assert "recorder_disk_low" not in _keys(RA.evaluate(text(*[row((3 - i) * 60, recorder_state="RECORDING", free_mb=100) for i in range(3)]), NOW))


def test_an_absurd_or_non_finite_free_space_is_not_a_reading_and_never_crashes_the_evaluation():
    rows = [row(180, recorder_state="RECORDING"), row(120, recorder_state="RECORDING"), row(60, recorder_state="RECORDING")]
    for bad in ("1" + "0" * 400, "-Infinity", "NaN"):
        lines = [json.dumps({**r, "recorder_free_mb": 0}).replace('"recorder_free_mb": 0', '"recorder_free_mb": ' + bad) if i == 2 else json.dumps({**r, "recorder_free_mb": 100})
                 for i, r in enumerate(rows)]
        RA.evaluate("\n".join(lines) + "\n", NOW)                                                  # must not raise (OverflowError used to escape and kill a CRITICAL order_sent alert with it)


def test_the_alert_reserve_is_the_recorders_own_default_reserve():
    """The warning is defined relative to the reserve the recorder actually enforces (``build(min_free_mb=512)``): if that default moves, this alert's arithmetic must move with it."""
    import inspect
    import market_recorder
    default = inspect.signature(market_recorder.build).parameters["min_free_mb"].default
    assert default == RA.RECORDER_RESERVE_MB == 512
    assert RA.RECORDER_FREE_WARN_MB == RA.RECORDER_RESERVE_MB + 1024


# ---------------------------------------------------------------- a failed READ is not "no data" (an independent review: an expired token and an empty history both said `no_data`)
def _by_key(alerts):
    return {a.key: a for a in alerts}


@pytest.mark.parametrize("klass,key,severity", [
    ("auth_failed", "watcher_credentials", "WARNING"),            # the watcher's own token is dead: fix the secret, the machine may be fine
    ("machine_unreachable", "machine_unreachable", "CRITICAL"),   # fly ssh could not reach the machine at all: down, stopped or a Fly outage; must not look like an empty history
    ("timeout", "machine_unreachable", "CRITICAL"),
    ("unknown", "read_failed", "CRITICAL"),           # a blind watcher that cannot even say why is not a warning
])
def test_a_failed_history_read_is_reported_as_what_it_is_and_never_as_no_data(klass, key, severity):
    alerts = RA.evaluate("", 1_000_000.0, read_failure=klass)
    got = _by_key(alerts)
    assert key in got and got[key].severity == severity, [(a.key, a.severity) for a in alerts]
    assert "no_data" not in got, "a read failure was called 'no data'"
    assert klass in got[key].text or "cannot read" in got[key].text.lower()


def test_an_empty_history_with_no_read_failure_is_still_no_data():
    got = _by_key(RA.evaluate("", 1_000_000.0))
    assert "no_data" in got and not {"watcher_credentials", "machine_unreachable", "read_failed"} & set(got)


def test_a_read_failure_class_outside_the_closed_set_is_refused_not_echoed():
    """The class travels from a shell step into an alert that goes to Slack: only a closed vocabulary is accepted, so error text (which can contain a token fragment) can never reach it."""
    secret = "FlyV1 fm2_" + "x" * 60
    got = _by_key(RA.evaluate("", 1_000_000.0, read_failure=secret))
    assert "read_failed" in got and secret not in got["read_failed"].text and "x" * 20 not in got["read_failed"].text


def test_the_cli_takes_the_read_failure_class_and_still_exits_nonzero(tmp_path, capsys):
    hist = tmp_path / "history.jsonl"
    hist.write_text("")
    code = RA.main([str(hist), "--dry-run", "--read-failure", "machine_unreachable"], env={})
    out = capsys.readouterr().out
    assert code != 0 and "[CRITICAL] machine_unreachable" in out and "no_data" not in out


def test_a_failed_read_distrusts_the_text_that_arrived_but_never_hides_an_alert_that_does_not_come_from_it():
    """Review: the old test was named 'does not hide other alerts' but asserted only that the history was ignored, on one key. The two halves of the contract, each with its own control:
    (1) whatever text arrived with a failed read is NOT trusted as a history (an order alarm in it raises nothing, and nothing is called no_data);
    (2) an alert that does not come from the history (the forward-shadow evidence) is still raised next to the read failure."""
    alarm = json.dumps({"order_sent": True, "cycle": 1})
    control = _by_key(RA.evaluate(alarm, 1_000_000.0))                                          # the same text READ fine is an ORDER ALARM
    assert "order_sent" in control and "machine_unreachable" not in control
    for failure, key in (("timeout", "machine_unreachable"), ("machine_unreachable", "machine_unreachable"), ("auth_failed", "watcher_credentials"), ("unknown", "read_failed"), ("nonsense", "read_failed")):
        got = _by_key(RA.evaluate(alarm, 1_000_000.0, read_failure=failure))
        assert key in got and set(got) == {key}, (failure, sorted(got))                         # (1): only the read failure; no order_sent, no no_data, no stale, no problem:*
    forward = {"state": "STALE", "path": "forward.json", "reasons": ["the evidence file is old"]}
    both = _by_key(RA.evaluate(alarm, 1_000_000.0, read_failure="timeout", forward=forward))
    assert set(both) == {"machine_unreachable", "forward_stale"}, sorted(both)                  # (2): the independent alert is NOT hidden by the failed read
    assert "the evidence file is old" in both["forward_stale"].text
    assert set(_by_key(RA.evaluate(alarm, 1_000_000.0, read_failure="timeout", forward={"state": "OK"}))) == {"machine_unreachable"}, "an OK forward status adds nothing"


# -- Round 20: a volume that never recovers is not the same alarm as a dip that will ----------------------------

def _hourly(hours, mb, *, step_h=1.0):
    """Readings every `step_h` hours ending NOW, each `mb` (a number, or a callable of hours-ago)."""
    n = int(hours / step_h)
    return [row(int((n - i) * step_h * 60) - 0, recorder_state="FAULT", recorder_free_mb=(mb(int((n - i) * step_h)) if callable(mb) else mb)) for i in range(n + 1)]


def test_a_shortfall_that_outlasts_two_utc_midnights_names_both_causes_and_the_read_only_checks():
    """REPRODUCED (Round 20, specialist A): a 1 GB volume cannot hold one day of depth above the 512 MiB reserve, so the recorder never writes again, yet the alert was byte-identical to the one for a transient
    dip. The live history (510.5 MiB hourly since 2026-11-11T12:28Z) crosses this line at 59 h. The alert fires on the history alone and deletes nothing."""
    alerts = RA.evaluate(text(*_hourly(59, 510.5)), NOW)
    assert "recorder_stall_permanent" in _keys(alerts) and "recorder_disk_low" in _keys(alerts) and "recorder_down" in _keys(alerts)
    msg = next(a for a in alerts if a.key == "recorder_stall_permanent")
    assert msg.severity == RA.WARNING and "UTC midnights" in msg.text and "cannot hold even ONE day" in msg.text and "predates the bounded recovery" in msg.text
    assert "fly volumes list -a carry-drill" in msg.text and "ls -l --time-style=full-iso /data/market" in msg.text and "Nothing is deleted or resized" in msg.text


@pytest.mark.parametrize("hours", [10, 36, 47])
def test_a_shortfall_shorter_than_48_hours_is_never_called_permanent(hours):
    assert "recorder_stall_permanent" not in _keys(RA.evaluate(text(*_hourly(hours, 510.5)), NOW))


def test_a_recovery_resets_the_run_and_a_healthy_history_never_fires():
    below = _hourly(60, 510.5)
    recovered = [dict(r) for r in below]
    recovered[30]["recorder_free_mb"] = 900                       # one reading at or above the reserve ends the run: only ~30 h remain after it
    assert "recorder_stall_permanent" not in _keys(RA.evaluate(text(*recovered), NOW))
    healthy = _hourly(80, lambda h: 5000 - (h % 24) * 100)
    assert "recorder_stall_permanent" not in _keys(RA.evaluate(text(*healthy), NOW))


def test_unusable_readings_are_skipped_and_a_real_reading_above_the_reserve_still_ends_the_run():
    rows = [dict(r) for r in _hourly(59, 510.5)]
    for k, junk in zip(range(1, len(rows) - 3, 7), [True, float("nan"), "510", None, [], 1e999, {}, False]):
        rows[k]["recorder_free_mb"] = junk                          # not readings: skipped, neither extending nor ending the run
    assert "recorder_stall_permanent" in _keys(RA.evaluate(text(*rows), NOW))
    rows[-5]["recorder_free_mb"] = 5000.0                           # a REAL reading above the reserve four hours ago
    assert "recorder_stall_permanent" not in _keys(RA.evaluate(text(*rows), NOW))


def test_the_permanent_alert_needs_stamps_and_never_reads_the_machines_clock():
    rows = [dict(r) for r in _hourly(59, 510.5)]
    for r in rows:
        r["at"] = "garbage"
    assert "recorder_stall_permanent" not in _keys(RA.evaluate(text(*rows), NOW)), "unreadable stamps cannot establish a duration"


# -- Round 22: image provenance and the early warning in hours --------------------------------------------------

def _img_rows(*statuses):
    n = len(statuses)
    return [row((n - i) * 60, recorder_state="RECORDING", recorder_image_status=s) for i, s in enumerate(statuses)]


def test_a_recorder_image_whose_provenance_is_not_established_for_three_cycles_raises_recorder_image_mismatch():
    for bad in ("MISMATCH", "MISSING"):
        alerts = RA.evaluate(text(*_img_rows("VERIFIED", bad, bad, bad)), NOW)
        assert "recorder_image_mismatch" in _keys(alerts) and "recorder_down" not in _keys(alerts), bad          # it is still RECORDING: this is a different fault
        msg = next(a for a in alerts if a.key == "recorder_image_mismatch")
        assert msg.severity == RA.WARNING and "BUILD_COMMIT" in msg.text and "Nothing is changed" in msg.text


def test_one_or_two_bad_image_cycles_a_verified_image_or_no_field_do_not_alert():
    for sts in (("VERIFIED", "MISMATCH", "MISMATCH"), ("MISMATCH", "MISMATCH", "VERIFIED"), ("MISMATCH", "MISSING"), ("VERIFIED", "VERIFIED", "VERIFIED"), ("NOT_CHECKED",) * 3):
        assert "recorder_image_mismatch" not in _keys(RA.evaluate(text(*_img_rows(*sts)), NOW)), sts
    assert "recorder_image_mismatch" not in _keys(RA.evaluate(text(row(180), row(120), row(60)), NOW))


def _soon_rows(*hours, health="degraded"):
    n = len(hours)
    return [row((n - i) * 60, recorder_state="RECORDING", recorder_health=health, recorder_hours_to_reserve=h) for i, h in enumerate(hours)]


def test_a_volume_that_reaches_the_reserve_within_a_day_warns_in_hours_before_the_headroom_is_gone():
    alerts = RA.evaluate(text(*_soon_rows(30.0, 20.0, 15.5, 11.0)), NOW)
    assert "recorder_reserve_soon" in _keys(alerts) and "recorder_down" not in _keys(alerts)
    msg = next(a for a in alerts if a.key == "recorder_reserve_soon")
    assert "11.0 h" in msg.text and "nothing is deleted or resized" in msg.text


def test_the_hours_warning_is_quiet_when_there_is_time_when_blocked_already_or_the_estimate_is_unusable():
    assert "recorder_reserve_soon" not in _keys(RA.evaluate(text(*_soon_rows(60.0, 48.0, 30.0)), NOW))
    assert "recorder_reserve_soon" not in _keys(RA.evaluate(text(*_soon_rows(10.0, 9.0, 8.0, health="storage_blocked")), NOW)), "blocked is recorder_down / recorder_disk_low's case"
    assert "recorder_reserve_soon" not in _keys(RA.evaluate(text(*_soon_rows(10.0, 9.0)), NOW)), "two cycles are not three"
    junk = [row(180, recorder_hours_to_reserve=True), row(120, recorder_hours_to_reserve="5"), row(60, recorder_hours_to_reserve=-3)]
    assert "recorder_reserve_soon" not in _keys(RA.evaluate(text(*junk), NOW))


def _resume_rows(days, health="storage_blocked", n=3, gb=4.4e9):
    return [row((n - i) * 60, recorder_state="STALE", recorder_health=health, recorder_resume_prune_days=days, recorder_resume_prune_bytes=gb) for i in range(n)]


def test_a_blocked_recorder_whose_first_write_would_delete_retained_days_says_so_before_the_owner_extends_the_volume():
    for health in ("storage_blocked", "stalled", "unknown"):
        alerts = RA.evaluate(text(*_resume_rows(3, health)), NOW)
        assert "recorder_resume_will_prune" in _keys(alerts), health
    msg = next(a for a in RA.evaluate(text(*_resume_rows(3)), NOW) if a.key == "recorder_resume_will_prune")
    assert msg.severity == RA.WARNING and "3 retained closed day(s) (4.40 GB)" in msg.text and "preserve them" in msg.text and "Nothing is deleted or resized" in msg.text


def test_the_resume_prune_alert_is_quiet_for_a_healthy_writer_for_zero_days_for_two_cycles_or_for_junk():
    assert "recorder_resume_will_prune" not in _keys(RA.evaluate(text(*_resume_rows(3, health="healthy")), NOW)), "a healthy writer's own expiry is normal"
    assert "recorder_resume_will_prune" not in _keys(RA.evaluate(text(*_resume_rows(0)), NOW))
    assert "recorder_resume_will_prune" not in _keys(RA.evaluate(text(*_resume_rows(3, n=2)), NOW)), "two cycles are not three"
    assert "recorder_resume_will_prune" not in _keys(RA.evaluate(text(*_resume_rows(True)), NOW)) and "recorder_resume_will_prune" not in _keys(RA.evaluate(text(*_resume_rows("3")), NOW))


def test_the_disk_alerts_use_the_reserve_the_recorder_reports_not_a_hard_coded_512_mib():
    """REPRODUCED (Round 22 fly review): runner_alerts compared against a constant 512 MiB. A recorder running with a 2 GiB reserve and 1.5 GiB free is BLOCKED; the alert must see it."""
    blocked = [row((3 - i) * 60, recorder_state="STALE", recorder_free_mb=1536.0, recorder_reserve_bytes=2 * 1024 ** 3) for i in range(3)]
    msg = next(a for a in RA.evaluate(text(*blocked), NOW) if a.key == "recorder_disk_low")
    assert "BELOW the 2048 MiB write reserve" in msg.text
    # a smaller reserve is not over-alerted: 1.2 GiB free is far above a 64 MiB reserve + 1 GiB headroom? (64 + 1024 = 1088 < 1229)
    fine = [row((3 - i) * 60, recorder_state="RECORDING", recorder_free_mb=1229.0, recorder_reserve_bytes=64 * 1024 ** 2) for i in range(3)]
    assert "recorder_disk_low" not in _keys(RA.evaluate(text(*fine), NOW))
    # a row that does not say falls back to the documented 512 MiB
    legacy = [row((3 - i) * 60, recorder_state="RECORDING", recorder_free_mb=1400.0) for i in range(3)]
    assert "recorder_disk_low" in _keys(RA.evaluate(text(*legacy), NOW))
