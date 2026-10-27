"""The Slack connection is reliable: retried, never silently lost, and provably alive.

Healthy is silent, so a dropped alert or a dead connection must not look the same
as a healthy runner. The URLs here are SYNTHETIC (assembled from parts).
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

import runner_alerts as RA  # noqa: E402
import runner_monitor as RM  # noqa: E402
from test_runner_alerts import (ENV, FAKE_URL, NOW, TOKEN, Log, Wire,  # noqa: E402
                                _loop, row, text)


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    monkeypatch.setattr(RA, "RETRY_BACKOFF_S", (0.0, 0.0))


class Flaky:
    """Fails with the given outcomes in order (an int is a status, an Exception
    is raised), then answers 200."""

    def __init__(self, *script):
        self.script, self.calls, self.sent = list(script), 0, []

    def __call__(self, url, body, timeout):
        self.calls += 1
        step = self.script.pop(0) if self.script else 200
        if isinstance(step, BaseException):
            raise step
        if step == 200:
            self.sent.append(json.loads(body.decode("utf-8")))
        return step


class Selective:
    """Answers `status` (`times` times) to any message whose text contains
    `needle`, 200 to everything else; records what was delivered."""

    def __init__(self, needle, status=503, times=3):
        self.needle, self.status, self.times = needle, status, times
        self.sent, self.refused = [], 0

    def __call__(self, url, body, timeout):
        msg = json.loads(body.decode("utf-8"))
        if self.needle in msg["text"] and self.refused < self.times:
            self.refused += 1
            return self.status
        self.sent.append(msg)
        return 200


# -- retries ------------------------------------------------------------------

class TestRetries:
    def test_a_timeout_then_a_503_then_success_is_one_delivered_alert(self):
        wire, naps = Flaky(TimeoutError("t"), 503), []
        n = RA.Notifier(ENV, transport=wire, say=Log(), sleep=naps.append)
        assert n.send(RA.Alert("k", RA.CRITICAL, "x")) == "sent"
        assert wire.calls == 3 and len(wire.sent) == 1

    def test_it_backs_off_between_attempts(self, monkeypatch):
        monkeypatch.setattr(RA, "RETRY_BACKOFF_S", (0.5, 1.5))
        naps = []
        n = RA.Notifier(ENV, transport=Flaky(503, 503), say=Log(), sleep=naps.append)
        n.send(RA.Alert("k", RA.WARNING, "x"))
        assert naps == [0.5, 1.5]

    def test_a_rate_limit_is_retried(self):
        wire = Flaky(429)
        assert RA.Notifier(ENV, transport=wire, say=Log(),
                           sleep=lambda s: None).send(RA.Alert("k", RA.INFO, "x")) == "sent"
        assert wire.calls == 2

    def test_attempts_are_bounded_and_the_failure_is_reported(self):
        wire, log = Flaky(*[500] * 10), Log()
        n = RA.Notifier(ENV, transport=wire, say=log, sleep=lambda s: None)
        out = n.send(RA.Alert("k", RA.CRITICAL, "x"))
        assert out == "failed: HTTP 500" and wire.calls == RA.ATTEMPTS == 3
        assert "after 3 attempts" in log.joined()

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 410])
    def test_a_rejection_is_not_retried_and_says_to_check_the_webhook(self, status):
        wire, log = Flaky(status, status, status), Log()
        n = RA.Notifier(ENV, transport=wire, say=log, sleep=lambda s: None)
        assert n.send(RA.Alert("k", RA.CRITICAL, "x")) == f"failed: HTTP {status}"
        assert wire.calls == 1
        assert "rejected" in log.joined().lower() and "webhook" in log.joined().lower()
        assert TOKEN not in log.joined() and FAKE_URL not in log.joined()

    def test_a_stop_signal_is_never_retried_or_swallowed(self):
        import unattended_drill as UD
        n = RA.Notifier(ENV, transport=Flaky(UD.Shutdown("SIGTERM")), say=Log(),
                        sleep=lambda s: None)
        with pytest.raises(UD.Shutdown):
            n.send(RA.Alert("k", RA.CRITICAL, "x"))

    def test_the_total_wait_is_short_enough_not_to_stall_a_cycle(self):
        assert sum(RA.RETRY_BACKOFF_S_DEFAULT) <= 5.0
        assert RA.TIMEOUT_S * RA.ATTEMPTS + sum(RA.RETRY_BACKOFF_S_DEFAULT) <= 30.0


# -- a send that failed is not a send that happened ---------------------------

class TestAFailedSendIsRetriedNextCycle:
    def test_the_throttle_forgets_an_alert_that_was_not_delivered(self):
        t = RA.Throttle(resend_s=3600)
        a = RA.Alert("stale", RA.CRITICAL, "x")
        assert t.plan([a], 0.0) == [a]
        t.unsent(a)
        assert t.plan([a], 60.0) == [a]               # not suppressed for 6 hours

    def test_a_delivered_alert_stays_throttled(self):
        t = RA.Throttle(resend_s=3600)
        a = RA.Alert("stale", RA.CRITICAL, "x")
        t.plan([a], 0.0)
        assert t.plan([a], 60.0) == []

    def test_an_undelivered_recovery_is_tried_again(self):
        t = RA.Throttle(resend_s=3600)
        a = RA.Alert("stale", RA.CRITICAL, "x")
        t.plan([a], 0.0)
        rec = t.plan([], 60.0)
        assert [r.key for r in rec] == ["stale:recovered"]
        t.unsent(rec[0])
        again = t.plan([], 120.0)
        assert [r.key for r in again] == ["stale:recovered"]
        assert t.plan([], 180.0) == []                # once it is delivered it is done

    def test_in_the_loop_a_page_lost_to_a_dead_connection_arrives_when_it_returns(self, tmp_path):
        # the needle is the PAGE's wording, which the startup message does not contain
        wire = Selective("monitor cycles in a row", status=503, times=3)   # one whole send fails
        n = RA.Notifier(ENV, transport=wire, say=Log(), sleep=lambda s: None)
        _loop(tmp_path, n, ["MONITOR_FAILED", "MONITOR_FAILED", "MONITOR_FAILED"])
        pages = [m["text"] for m in wire.sent if "monitor cycles in a row" in m["text"]]
        assert wire.refused == 3 and len(pages) == 1   # lost once, delivered on a later cycle


# -- proof of life ------------------------------------------------------------

class TestAnnounce:
    def test_the_loop_announces_that_alerts_are_on(self, tmp_path):
        wire = Flaky()
        n = RA.Notifier(ENV, transport=wire, say=Log(), sleep=lambda s: None)
        os.makedirs(tmp_path, exist_ok=True)
        RM._Alerts(str(tmp_path), n, lambda: NOW).announce()
        assert len(wire.sent) == 1
        t = wire.sent[0]["text"]
        assert "[INFO]" in t and "alerts are ON" in t and "150" in t

    def test_a_disabled_notifier_announces_nothing_but_says_so_loudly(self, tmp_path):
        os.makedirs(tmp_path, exist_ok=True)
        RM._Alerts(str(tmp_path), RA.Notifier({}, transport=Wire(), say=Log()),
                   lambda: NOW).announce()
        assert "ALERTS DISABLED" in open(os.path.join(tmp_path, "orchestrator.log")).read()

    def test_a_failing_announcement_cannot_break_the_monitor(self, tmp_path):
        os.makedirs(tmp_path, exist_ok=True)

        class Exploding(RA.Notifier):
            def send(self, alert):
                raise RuntimeError("bug " + FAKE_URL)
        RM._Alerts(str(tmp_path), Exploding(ENV, transport=Wire(), say=Log()),
                   lambda: NOW).announce()
        log = open(os.path.join(tmp_path, "orchestrator.log")).read()
        assert "alert startup failed" in log and TOKEN not in log

    def test_loop_announces_once_per_start_and_the_exit_code_is_unchanged(self, tmp_path):
        wire = Flaky()
        n = RA.Notifier(ENV, transport=wire, say=Log(), sleep=lambda s: None)
        code = RM.loop(str(tmp_path), env={}, interval_s=0.0, max_runs=1,
                       clock=lambda: NOW, sleep=lambda s: None,
                       once=lambda *a, **k: 0, notifier=n)
        assert code == 0
        assert sum(m["text"].count("alerts are ON") for m in wire.sent) == 1


class TestDigest:
    def test_one_digest_a_day_not_before(self, tmp_path):
        wire = Flaky()
        n = RA.Notifier(ENV, transport=wire, say=Log(), sleep=lambda s: None)
        _loop(tmp_path / "a", n, ["ENTRY_NOT_ELIGIBLE"] * 23)
        assert not [m for m in wire.sent if "daily digest" in m["text"]]
        wire2 = Flaky()
        n2 = RA.Notifier(ENV, transport=wire2, say=Log(), sleep=lambda s: None)
        _loop(tmp_path / "b", n2, ["ENTRY_NOT_ELIGIBLE"] * 25)
        digests = [m for m in wire2.sent if "daily digest" in m["text"]]
        assert len(digests) == 1

    def test_the_digest_says_what_it_is_and_what_it_is_not(self, tmp_path):
        wire = Flaky()
        n = RA.Notifier(ENV, transport=wire, say=Log(), sleep=lambda s: None)
        _loop(tmp_path, n, ["ENTRY_NOT_ELIGIBLE"] * 25)
        d = [m["text"] for m in wire.sent if "daily digest" in m["text"]][0]
        assert d.startswith("[INFO]")
        assert "cycles" in d and "outcomes" in d
        assert "none is evidence of profit" in d
        assert TOKEN not in d and FAKE_URL not in d

    def test_a_digest_that_could_not_be_delivered_is_tried_again(self, tmp_path):
        wire = Selective("daily digest", status=503, times=3)
        n = RA.Notifier(ENV, transport=wire, say=Log(), sleep=lambda s: None)
        _loop(tmp_path, n, ["ENTRY_NOT_ELIGIBLE"] * 26)
        assert wire.refused == 3
        assert len([m for m in wire.sent if "daily digest" in m["text"]]) == 1

    def test_a_dead_runner_digest_is_not_needed_the_stale_page_covers_it(self):
        # the digest is a heartbeat; it adds no new failure mode to evaluate()
        assert RA.evaluate(text(row(5)), NOW) == []


# -- the connection test from a terminal --------------------------------------

class TestConnectionTest:
    def test_it_sends_one_labelled_test_message_and_exits_zero(self, capsys, monkeypatch):
        wire = Flaky()
        monkeypatch.setattr(RA, "post", wire)
        code = RA.main(["--test"], env=ENV)
        out = capsys.readouterr()
        assert code == 0 and len(wire.sent) == 1
        assert "TEST" in wire.sent[0]["text"] and "[INFO]" in wire.sent[0]["text"]
        assert "sent" in out.out and TOKEN not in out.out + out.err

    def test_a_missing_webhook_exits_two_and_says_so(self, capsys):
        code = RA.main(["--test"], env={})
        assert code == 2 and RA.ENV_VAR in capsys.readouterr().err

    def test_a_rejected_webhook_exits_one_and_names_the_check(self, capsys, monkeypatch):
        monkeypatch.setattr(RA, "post", Flaky(404))
        code = RA.main(["--test"], env=ENV)
        err = capsys.readouterr()
        assert code == 1 and "404" in err.out + err.err
        assert TOKEN not in err.out + err.err

    def test_a_history_is_still_required_without_test(self):
        with pytest.raises(SystemExit):
            RA.main([], env=ENV)
