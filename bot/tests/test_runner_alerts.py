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
        assert len(wire.sent) == 1 and "FAILED" in wire.sent[0][1]["text"]

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
        assert wire.sent == []


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


# -- it can read, it cannot trade ---------------------------------------------

def test_the_module_has_no_order_capable_import_and_no_credentials():
    src = open(os.path.join(BOT, "tools", "runner_alerts.py"), encoding="utf-8").read()
    for banned in ("carry_broker", "bybit_connection", "place_market", "requests",
                   "BYBIT_API", "LIVE_TRADING_ACK", "place_order"):
        assert banned not in src, banned
