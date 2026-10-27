#!/usr/bin/env python3
"""Slack alerts for the deployed monitor: says so when the runner stops being well.

It READS the monitor's history and POSTS one JSON message to a Slack incoming
webhook. It places no order, holds no exchange credential and imports no
order-capable client. It cannot change a monitor outcome, exit code or order
gate: every failure inside it is contained and reported in the monitor's own
log, and a stop signal (a BaseException) still passes through.

THE WEBHOOK URL IS A SECRET. It is read only from SLACK_WEBHOOK_URL. It is never
printed, logged, returned or raised: every exception text that could carry it is
passed through `redact` first, and only the exception TYPE is logged. A URL that
is not `https://hooks.slack.com/services/...` is refused, so the variable cannot
be pointed at another host to carry the history away.

WHAT IT ALERTS ON
  order_sent    a history line that does not say order_sent=false (CRITICAL,
                never throttled: the monitor cannot send orders)
  stale         no cycle within the stale limit (CRITICAL)
  hung          the running loop itself has finished no cycle within the limit
                (CRITICAL; a watchdog thread inside the monitor)
  failed_streak MONITOR_FAILED on the last N cycles in a row (WARNING)
  shadow        the last forward-shadow step was FAULT or REFUSED (WARNING)
  no_data       the history has no readable cycle (WARNING)

WHAT IT CANNOT DO. A process or machine that has died cannot report its own
death. The in-process notifier covers a failing, faulting or HUNG monitor. For a
dead one, run this module's command line from somewhere that is not the runner,
on a schedule, against the runner's history:

    fly ssh console -a carry-drill -C "cat /data/monitor/monitor.jsonl" \\
        | python3 tools/runner_alerts.py -          # SLACK_WEBHOOK_URL in env

Exit code is the health report's (0 healthy, 1 problem, 2 no data, 3 alarm).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, Dict, List, Mapping, Optional, Sequence

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import monitor_summary as MS  # noqa: E402

ENV_VAR = "SLACK_WEBHOOK_URL"
ALLOWED_HOST = "hooks.slack.com"
ALLOWED_PATH_PREFIX = "/services/"
TIMEOUT_S = 5.0
MAX_TEXT = 1500
STALE_MINUTES = 150.0
FAILED_STREAK = 2
RESEND_S = 6 * 3600.0
WATCHDOG_POLL_S = 60.0

CRITICAL = "CRITICAL"
WARNING = "WARNING"
RECOVERED = "RECOVERED"

#: Never throttled: a line that is not order_sent=false pages every time.
UNTHROTTLED = frozenset({"order_sent"})


@dataclass(frozen=True)
class Alert:
    key: str
    severity: str
    text: str


# -- the secret ---------------------------------------------------------------

_ANY_SLACK_URL = re.compile(r"https?://\S*hooks\.slack\.com\S*", re.IGNORECASE)


def redact(text: str, secret: Optional[str] = None) -> str:
    """`text` with the webhook (and every Slack URL, and the URL's path pieces)
    replaced. Applied to anything that came from an exception or a transport."""
    out = str(text)
    if secret:
        out = out.replace(secret, "[redacted]")
        for piece in urllib.parse.urlsplit(secret).path.split("/"):
            if len(piece) >= 6:
                out = out.replace(piece, "[redacted]")
    return _ANY_SLACK_URL.sub("[redacted]", out)


_PATH = re.compile(r"/services/[A-Za-z0-9]+/[A-Za-z0-9]+/[A-Za-z0-9]+")


def slack_escape(text: str) -> str:
    """Slack's documented escaping. History text (reasons, shadow detail) is
    only indirectly under anyone's control, but `<!channel>` and `<url|label>`
    in it must not become a workspace ping or a fake link."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def valid_webhook(url: str) -> bool:
    """Exactly https://hooks.slack.com/services/<A>/<B>/<C> with plain
    alphanumeric segments: no query, fragment, userinfo, port, dot-segments,
    percent-escapes, empty segments or whitespace."""
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    return (parts.scheme == "https" and parts.hostname == ALLOWED_HOST
            and port is None and not parts.username and not parts.password
            and not parts.query and not parts.fragment
            and _PATH.fullmatch(parts.path) is not None
            and "?" not in url and "#" not in url)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):               # noqa: D401
        return None


def post(url: str, body: bytes, timeout: float) -> int:
    """POST `body` to a Slack webhook; the HTTP status. Refuses any other URL."""
    if not valid_webhook(url):
        raise ValueError("not a Slack webhook URL")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=timeout) as resp:         # noqa: S310
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)


# -- the notifier -------------------------------------------------------------

def _default_transport(url: str, body: bytes, timeout: float) -> int:
    return post(url, body, timeout)          # looked up at call time (tests patch it)


class Notifier:
    def __init__(self, env: Mapping[str, str],
                 transport: Optional[Callable[[str, bytes, float], int]] = None,
                 say: Callable[[str], None] = print) -> None:
        self._say = say
        self._transport = transport or _default_transport
        raw = (env.get(ENV_VAR) or "").strip()
        self._url: Optional[str] = None
        self.disabled_reason: Optional[str] = None
        if not raw:
            self.disabled_reason = f"{ENV_VAR} is not set: alerts are OFF"
        elif not valid_webhook(raw):
            self.disabled_reason = (f"{ENV_VAR} is set but is not an "
                                    f"https://{ALLOWED_HOST}/services/... "
                                    "webhook: alerts are OFF")
        else:
            self._url = raw
        self._app = (env.get("FLY_APP_NAME") or "").strip()

    @property
    def enabled(self) -> bool:
        return self._url is not None

    def __repr__(self) -> str:                  # never the URL
        return f"Notifier(enabled={self.enabled})"

    def send(self, alert: Alert) -> str:
        """'sent', 'disabled' or 'failed: <reason>'. Never raises an Exception
        and never carries the URL; a BaseException (a stop signal) passes."""
        if self._url is None:
            return "disabled"
        head = f"[{alert.severity}]" + (f" {self._app}" if self._app else "")
        text = slack_escape(redact(f"{head} {alert.text}", self._url))[:MAX_TEXT]
        body = json.dumps({"text": text}).encode("utf-8")
        try:
            status = self._transport(self._url, body, TIMEOUT_S)
        except Exception as exc:                               # noqa: BLE001
            self._say("alert send failed ("
                      + redact(f"{type(exc).__name__}", self._url)
                      + "); the monitor is unaffected")
            return f"failed: {type(exc).__name__}"
        if status != 200:
            self._say(f"alert send failed (HTTP {status}); the monitor is unaffected")
            return f"failed: HTTP {status}"
        return "sent"


# -- what is worth an alert ---------------------------------------------------

def _failed_streak(rows: Sequence[Mapping[str, object]]) -> int:
    n = 0
    for r in reversed(rows):
        if r.get("outcome") != "MONITOR_FAILED":
            break
        n += 1
    return n


def evaluate(history_text: str, now: float, *,
             stale_minutes: float = STALE_MINUTES,
             failed_streak: int = FAILED_STREAK) -> List[Alert]:
    """The alerts a history deserves right now. Pure: no clock, no network."""
    return _evaluate(history_text, now, stale_minutes, failed_streak)[0]


def _evaluate(history_text, now, stale_minutes, failed_streak):
    rows, _ = MS.scan(history_text)
    s = MS.summarise(rows, now=now, stale_minutes=stale_minutes,
                     raw_text=history_text)
    alerts: List[Alert] = []
    if s["exit"] == 3:
        alerts.append(Alert("order_sent", CRITICAL,
                            "ORDER ALARM: " + "; ".join(s.get("alarms", []))
                            + " The monitor cannot send orders: investigate NOW."))
    if s["exit"] == 2:
        alerts.append(Alert("no_data", WARNING,
                            "the monitor history has no readable cycle"))
    for problem in ([] if s["exit"] == 2 else s.get("problems", [])):
        if problem.startswith("the last cycle was MONITOR_FAILED"):
            continue                       # one failure is a blip; see the streak
        if problem.startswith("STALE"):
            alerts.append(Alert("stale", CRITICAL, problem))
        elif problem.startswith("the last shadow step"):
            alerts.append(Alert("shadow", WARNING, problem))
        else:
            alerts.append(Alert("problem:" + re.sub(r"\W+", "_", problem[:20]).strip("_"),
                                WARNING, problem))
    streak = _failed_streak(rows)
    if rows and streak >= failed_streak:
        why = "; ".join(map(str, rows[-1].get("reasons") or [])) or "no reason recorded"
        alerts.append(Alert("failed_streak", WARNING,
                            f"{streak} monitor cycles in a row ended MONITOR_FAILED: {why}"))
    return alerts, s


class Throttle:
    """Don't repeat an unchanged alert inside `resend_s`, and say once when a
    condition clears. `order_sent` is never throttled."""

    def __init__(self, resend_s: float = RESEND_S) -> None:
        self._resend_s = float(resend_s)
        self._last: Dict[str, float] = {}
        self._active: Dict[str, Alert] = {}
        self._lock = threading.Lock()

    def plan(self, alerts: Sequence[Alert], now: float) -> List[Alert]:
        out: List[Alert] = []
        with self._lock:
            current = {a.key: a for a in alerts}
            for key, a in current.items():
                last = self._last.get(key)
                if key in UNTHROTTLED or last is None or now - last >= self._resend_s:
                    self._last[key] = now
                    out.append(a)
            for key in list(self._active):
                if key not in current:
                    out.append(Alert(f"{key}:recovered", RECOVERED,
                                     f"{key} has cleared"))
                    self._last.pop(key, None)
            self._active = current
        return out


class Watchdog:
    """For a loop that is alive but stuck inside a cycle."""

    def __init__(self, stale_minutes: float = STALE_MINUTES,
                 clock: Callable[[], float] = time.time) -> None:
        self._limit_s = float(stale_minutes) * 60.0
        self._clock = clock
        self._last = clock()

    def beat(self) -> None:
        self._last = self._clock()

    def tick(self) -> Optional[Alert]:
        idle = self._clock() - self._last
        if idle > self._limit_s:
            return Alert("hung", CRITICAL,
                         f"the monitor loop has finished no cycle in {idle / 60.0:.0f} "
                         f"min (limit {self._limit_s / 60.0:.0f}): it is stuck inside a cycle")
        return None


# -- command line, for a watcher that is NOT on the runner ---------------------

def main(argv: Optional[Sequence[str]] = None,
         env: Optional[Mapping[str, str]] = None) -> int:
    env = os.environ if env is None else env
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("history", help="monitor.jsonl, or - for stdin")
    ap.add_argument("--stale-minutes", type=float, default=STALE_MINUTES)
    ap.add_argument("--failed-streak", type=int, default=FAILED_STREAK)
    ap.add_argument("--now", type=float, default=None)
    ap.add_argument("--dry-run", action="store_true",
                    help="print the alerts; send nothing")
    args = ap.parse_args(argv)
    try:
        raw = sys.stdin.read() if args.history == "-" else \
            open(args.history, encoding="utf-8", errors="replace").read()
    except OSError as exc:
        print(f"cannot read {args.history}: {type(exc).__name__}", file=sys.stderr)
        return 2
    now = time.time() if args.now is None else args.now
    alerts, summary = _evaluate(raw, now, args.stale_minutes, args.failed_streak)
    code = max(int(summary["exit"]), 1 if alerts else 0)
    for a in alerts:
        print(f"[{a.severity}] {a.key}: {a.text}")
    if args.dry_run or not alerts:
        return code
    notifier = Notifier(env, say=lambda line: print(line, file=sys.stderr))
    if not notifier.enabled:
        print(f"ALERTS DISABLED: {notifier.disabled_reason}", file=sys.stderr)
        return code
    for a in alerts:
        print(f"alert {a.key}: {notifier.send(a)}", file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
