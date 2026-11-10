#!/usr/bin/env python3
"""Notification adapters for the independent health monitor. Standard library only.

    WebhookNotifier   JSON POST to an https URL the CALLER injects.
    TelegramNotifier  Bot API sendMessage with a token and chat id the CALLER injects.

NOTHING IS INVENTED: this module has no default recipient, URL, chat id or token and
reads NO environment (tests/test_environment_read_surface.py forbids new readers).
Absent configuration is an explicit ``NOT_CONFIGURED`` result, never a silent success;
the monitor turns it into a failing health check.

SECRETS: a webhook URL and a bot token are secrets. They are never put in an
exception text, a log line, a result detail or a repr. Failures report only an
exception TYPE or an HTTP status code. Message text is length-limited, stripped of
control characters and scanned with ``tools/secret_scan``; a flagged line is replaced
by ``[REDACTED]`` and any literal secret the adapter holds is removed.

TRANSPORT: https only (a loopback ``http://127.0.0.1`` endpoint is accepted solely
when the constructor flag ``allow_insecure_loopback_for_tests=True`` is passed), no
credentials embedded in the URL, redirects are REFUSED (never followed, so a secret
cannot be forwarded to another host), a bounded timeout and bounded retries
(network errors, HTTP 429 and 5xx; never 4xx).

DE-DUPLICATION: ``dispatch`` keeps ``state['active']`` (a dict the caller persists).
An incident that persists is re-sent only after ``renotify_interval_ms``; a recovery
message is sent once when it clears. State only advances for a message that at least
one adapter actually delivered, so a failed send is retried on the next run.
"""
from __future__ import annotations

import http.client
import json
import os
import re
import ssl
import tempfile
import time
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional, Sequence
from urllib.parse import urlsplit

try:  # the repo's credential-shape scanner (stdlib only); optional at import time
    from tools import secret_scan as _secret_scan  # type: ignore
except Exception:  # pragma: no cover - run as a script from tools/
    try:
        import secret_scan as _secret_scan  # type: ignore
    except Exception:
        _secret_scan = None

SENT = "SENT"
FAILED = "FAILED"
NOT_CONFIGURED = "NOT_CONFIGURED"

#: Telegram's hard limit is 4096 characters; stay well below it.
MAX_TEXT_CHARS = 3500
DEFAULT_TIMEOUT_S = 10.0
DEFAULT_RETRIES = 2
DEFAULT_RENOTIFY_INTERVAL_MS = 6 * 3600 * 1000
TELEGRAM_HOST = "api.telegram.org"
LOOPBACK_HOST = "127.0.0.1"

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_BEARERISH = re.compile(r"(?i)\b(bearer|apikey|api-key)\b[ \t:=]+[A-Za-z0-9._~+/=-]{12,}")
_TOKENISH = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{25,}\b")  # Telegram bot token shape


@dataclass(frozen=True)
class SendResult:
    status: str          # SENT | FAILED | NOT_CONFIGURED
    adapter: str
    detail: str = ""     # never contains a secret
    attempts: int = 0

    @property
    def ok(self) -> bool:
        return self.status == SENT


def sanitize(text: str, secrets: Iterable[str] = (), limit: int = MAX_TEXT_CHARS) -> str:
    """Length-limit, strip control characters, remove literal secrets, redact secret-shaped lines."""
    text = _CONTROL.sub(" ", str(text))
    for s in secrets:
        if s and len(s) >= 4:
            text = text.replace(s, "[REDACTED]")
    text = _TOKENISH.sub("[REDACTED]", text)
    text = _BEARERISH.sub("[REDACTED]", text)
    if _secret_scan is not None:
        try:
            flagged = {n for n, _rule in _secret_scan.scan_text(text)}
        except Exception:
            flagged = set()
        if flagged:
            lines = text.splitlines()
            text = "\n".join("[REDACTED]" if (i + 1) in flagged else ln for i, ln in enumerate(lines))
    if len(text) > limit:
        text = text[: max(0, limit - 15)] + "\n[...truncated]"
    return text


def _endpoint(url: str, *, allow_loopback: bool, what: str):
    """Validate an endpoint; raise ValueError with a FIXED text (never the URL)."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
    except Exception:
        raise ValueError(f"{what} is not a valid URL") from None
    if parts.username or parts.password:
        raise ValueError(f"{what} must not embed credentials")
    if not host:
        raise ValueError(f"{what} has no host")
    if parts.scheme == "https":
        return True, host, port or 443, (parts.path or "/") + (("?" + parts.query) if parts.query else "")
    if parts.scheme == "http" and allow_loopback and host == LOOPBACK_HOST:
        return False, host, port or 80, (parts.path or "/") + (("?" + parts.query) if parts.query else "")
    raise ValueError(f"{what} must be an https URL")


def _post_json(*, secure: bool, host: str, port: int, path: str, body: dict, timeout: float,
               retries: int, sleep: Callable[[float], None], adapter: str,
               ok_check: Optional[Callable[[bytes], bool]] = None) -> SendResult:
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    attempts = 0
    last = "no attempt"
    for i in range(max(0, int(retries)) + 1):
        attempts += 1
        conn = None
        try:
            if secure:
                conn = http.client.HTTPSConnection(host, port, timeout=timeout,
                                                   context=ssl.create_default_context())
            else:
                conn = http.client.HTTPConnection(host, port, timeout=timeout)
            conn.request("POST", path, body=payload,
                         headers={"Content-Type": "application/json; charset=utf-8",
                                  "User-Agent": "carry-health-monitor"})
            resp = conn.getresponse()
            data = resp.read(4096)
            status = resp.status
        except Exception as exc:  # only the TYPE is kept: the text could carry the URL
            last = f"network error ({type(exc).__name__})"
            status = None
            data = b""
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
        if status is not None:
            if 200 <= status < 300:
                if ok_check is None or ok_check(data):
                    return SendResult(SENT, adapter, f"HTTP {status}", attempts)
                return SendResult(FAILED, adapter, f"HTTP {status} but the service did not confirm", attempts)
            if 300 <= status < 400:
                return SendResult(FAILED, adapter, f"HTTP {status} redirect refused", attempts)
            last = f"HTTP {status}"
            if status != 429 and status < 500:
                return SendResult(FAILED, adapter, last, attempts)
        if i < retries:
            sleep(min(2.0 ** i, 5.0))
    return SendResult(FAILED, adapter, last, attempts)


class Notifier:
    """Interface: ``send(text) -> SendResult``. Subclasses must never raise secrets."""
    name = "notifier"

    def send(self, text: str) -> SendResult:  # pragma: no cover - interface
        raise NotImplementedError


class NotConfiguredNotifier(Notifier):
    name = "none"

    def __init__(self, reason: str = "no notifier configured"):
        self._reason = reason

    def send(self, text: str) -> SendResult:
        return SendResult(NOT_CONFIGURED, self.name, self._reason, 0)

    def __repr__(self) -> str:
        return "NotConfiguredNotifier()"


class WebhookNotifier(Notifier):
    name = "webhook"

    def __init__(self, url: str, *, timeout: float = DEFAULT_TIMEOUT_S, retries: int = DEFAULT_RETRIES,
                 allow_insecure_loopback_for_tests: bool = False,
                 sleep: Callable[[float], None] = time.sleep):
        self._secure, self._host, self._port, self._path = _endpoint(
            url, allow_loopback=allow_insecure_loopback_for_tests, what="webhook URL")
        self._secrets = (url, self._path if len(self._path) > 8 else "")
        self._timeout, self._retries, self._sleep = float(timeout), int(retries), sleep

    def send(self, text: str) -> SendResult:
        clean = sanitize(text, self._secrets)
        return _post_json(secure=self._secure, host=self._host, port=self._port, path=self._path,
                          body={"text": clean, "source": "fly-heartbeat-monitor"},
                          timeout=self._timeout, retries=self._retries, sleep=self._sleep,
                          adapter=self.name)

    def __repr__(self) -> str:
        return "WebhookNotifier(<configured>)"


class TelegramNotifier(Notifier):
    name = "telegram"

    def __init__(self, token: str, chat_id: str, *, timeout: float = DEFAULT_TIMEOUT_S,
                 retries: int = DEFAULT_RETRIES, api_base: Optional[str] = None,
                 allow_insecure_loopback_for_tests: bool = False,
                 sleep: Callable[[float], None] = time.sleep):
        if not token or not chat_id:
            raise ValueError("telegram token and chat id are both required")
        if not re.fullmatch(r"[A-Za-z0-9:_-]{8,200}", token):
            raise ValueError("telegram token has an invalid shape")
        if not re.fullmatch(r"-?[A-Za-z0-9@_]{1,64}", str(chat_id)):
            raise ValueError("telegram chat id has an invalid shape")
        if api_base is None:
            secure, host, port = True, TELEGRAM_HOST, 443
        else:
            secure, host, port, _p = _endpoint(api_base, allow_loopback=allow_insecure_loopback_for_tests,
                                               what="telegram api base")
            if secure and host != TELEGRAM_HOST:
                raise ValueError("telegram api base must be api.telegram.org")
        self._secure, self._host, self._port = secure, host, port
        self._token, self._chat = token, str(chat_id)
        self._timeout, self._retries, self._sleep = float(timeout), int(retries), sleep

    def send(self, text: str) -> SendResult:
        clean = sanitize(text, (self._token,))
        return _post_json(secure=self._secure, host=self._host, port=self._port,
                          path=f"/bot{self._token}/sendMessage",
                          body={"chat_id": self._chat, "text": clean, "disable_web_page_preview": True},
                          timeout=self._timeout, retries=self._retries, sleep=self._sleep,
                          adapter=self.name, ok_check=_telegram_ok)

    def __repr__(self) -> str:
        return "TelegramNotifier(<configured>)"


def _telegram_ok(data: bytes) -> bool:
    try:
        return json.loads(data.decode("utf-8")).get("ok") is True
    except Exception:
        return False


def build_notifiers(webhook_url: Optional[str], telegram_token: Optional[str],
                    telegram_chat_id: Optional[str], *, telegram_api_base: Optional[str] = None,
                    **kw) -> List[Notifier]:
    """Adapters for whatever the CALLER supplied. Empty inputs configure nothing.

    Returns ``[NotConfiguredNotifier]`` when nothing is configured. A half-configured Telegram
    (token without chat id or the reverse) counts as not configured. ValueError (fixed text,
    no secret) for a configured but invalid value.
    """
    out: List[Notifier] = []
    if webhook_url:
        out.append(WebhookNotifier(webhook_url, **kw))
    if telegram_token and telegram_chat_id:
        out.append(TelegramNotifier(telegram_token, telegram_chat_id, api_base=telegram_api_base, **kw))
    return out or [NotConfiguredNotifier()]


# ---------------------------------------------------------------- de-duplication state

def load_state(path: Optional[str]) -> dict:
    """Read the persisted state; a missing or corrupt file is an empty state (never an error)."""
    try:
        with open(path, encoding="utf-8") as h:  # type: ignore[arg-type]
            state = json.load(h)
        if isinstance(state, dict) and isinstance(state.get("active", {}), dict):
            state.setdefault("active", {})
            return state
    except Exception:
        pass
    return {"version": 1, "active": {}}


def save_state(path: str, state: dict) -> None:
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".notify-state-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as h:
            json.dump(state, h, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@dataclass(frozen=True)
class Alert:
    key: str
    severity: str
    text: str


@dataclass
class DispatchOutcome:
    results: List[SendResult]
    alerts_sent: List[str]
    recovered: List[str]

    @property
    def not_configured(self) -> bool:
        return bool(self.results) and all(r.status == NOT_CONFIGURED for r in self.results)

    @property
    def failed(self) -> bool:
        return any(r.status == FAILED for r in self.results)


def _send_all(notifiers: Sequence[Notifier], text: str):
    results = []
    for n in (list(notifiers) or [NotConfiguredNotifier()]):
        try:
            results.append(n.send(text))
        except Exception as exc:  # an adapter must not raise; if it does keep only the type
            results.append(SendResult(FAILED, getattr(n, "name", "notifier"),
                                      f"adapter error ({type(exc).__name__})", 0))
    return results, any(r.ok for r in results)


def dispatch(notifiers: Sequence[Notifier], alerts: Sequence[Alert], state: dict, now_ms: int,
             renotify_interval_ms: int = DEFAULT_RENOTIFY_INTERVAL_MS,
             allow_recovery: bool = True) -> DispatchOutcome:
    """Send what is new or due, and one recovery for what cleared; update ``state`` in place.

    ``allow_recovery=False`` is for a run that could not observe the system (an unreadable database
    must not read as "everything recovered").
    """
    active = state.setdefault("active", {})
    current = {a.key: a for a in alerts}

    def last_ms(key: str) -> int:
        """The persisted time of the last notification, SANITISED: a non-integer, negative or FUTURE value (a tampered state file) counts as never notified, so it can neither crash the
        monitor nor silence an alert forever."""
        v = active[key].get("last_notified_ms", 0) if isinstance(active.get(key), dict) else 0
        return v if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= now_ms else 0
    due = [a for a in alerts if a.key not in active or now_ms - last_ms(a.key) >= renotify_interval_ms]
    cleared = [k for k in active if k not in current] if allow_recovery else []
    results: List[SendResult] = []
    sent: List[str] = []
    recovered: List[str] = []
    if due:
        body = "HEALTH ALERT\n" + "\n".join(f"[{a.severity}] {a.text}" for a in due)
        res, ok = _send_all(notifiers, body)
        results += res
        if ok:
            for a in due:
                prev = active.get(a.key, {})
                active[a.key] = {"first_ms": int(prev.get("first_ms", now_ms)),
                                 "last_notified_ms": now_ms, "severity": a.severity}
                sent.append(a.key)
    if cleared:
        body = "HEALTH RECOVERED\n" + "\n".join(f"cleared: {k}" for k in sorted(cleared))
        res, ok = _send_all(notifiers, body)
        results += res
        if ok:
            for k in cleared:
                active.pop(k, None)
                recovered.append(k)
    return DispatchOutcome(results, sent, recovered)
