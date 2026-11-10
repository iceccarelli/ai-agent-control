"""Offline tests for tools/health_monitor.py, tools/notifier.py and the monitor workflow."""
from __future__ import annotations

import http.server
import io
import json
import os
import re
import sys
import threading

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))
import health_monitor as hm  # noqa: E402
import notifier as nt  # noqa: E402

REPO = os.path.dirname(os.path.dirname(HERE))
WF = os.path.join(REPO, ".github", "workflows", "fly-heartbeat-monitor.yml")

NOW = 1_800_000_000_000
TOKEN = "123456789:AAAbbbCCCdddEEEfffGGGhhhIIIjjjKKK_x"
KEY = "sb_monitor_KEY_abcdefghijklmnopqrstuvwxyz0123456789"


def hb(**kw):
    row = dict(worker_id="w1", app="carry", machine_id="m1", region="ams", code_revision="abc",
               runtime_version="3.11", run_id="run-1", mode="paper", state="WAITING_FOR_OPPORTUNITY",
               session_id=None, last_bybit_read_ms=NOW - 10_000, last_market_data_ms=NOW - 10_000,
               last_reconcile_ms=NOW - 60_000, last_supabase_sync_ms=NOW - 10_000, outbox_pending=0,
               disk_free_bytes=10 << 30, lease_token=None, seq=5, at_ms=NOW - 5_000)
    row.update(kw)
    return row


def codes(findings):
    return {f.code for f in findings}


def ev(code, severity="CRITICAL", detail="d", **kw):
    e = {"kind": "INCIDENT", "worker_id": "w1", "at_ms": NOW - 1000,
         "payload": {"code": code, "severity": severity, "detail": detail}}
    e.update(kw)
    return e


# ------------------------------------------------------------------ evaluation

def test_healthy_heartbeat_yields_nothing():
    assert hm.evaluate([hb()], [], NOW, {}) == []


def test_every_engine_state_is_recognised():
    for s in sorted(hm.ENGINE_STATES - {"ALERT", "UNRECONCILED", "STOPPED"}):
        extra = {"lease_token": 7} if s in hm.LEASE_STATES else {}
        assert "UNKNOWN" not in codes(hm.evaluate([hb(state=s, **extra)], [], NOW, {})), s


@pytest.mark.parametrize("code,rows,events", [
    ("HEARTBEAT_MISSING", [], []),
    ("HEARTBEAT_STALE", [hb(at_ms=NOW - 400_000)], []),
    ("NOT_FLAT", [hb(state="NOT_FLAT")], []),
    ("UNRECONCILED", [hb(state="UNRECONCILED")], []),
    ("ALERT", [hb(state="ALERT")], []),
    ("RISK_LIMIT_HIT", [hb()], [ev("RISK_LIMIT_DAILY_LOSS")]),
    ("BYBIT_READ_STALE", [hb(last_bybit_read_ms=NOW - 400_000)], []),
    ("MARKET_DATA_STALE", [hb(last_market_data_ms=NOW - 400_000)], []),
    ("RECONCILE_STALE", [hb(last_reconcile_ms=NOW - 3_600_000)], []),
    ("SUPABASE_SYNC_STALE", [hb(last_supabase_sync_ms=NOW - 900_000)], []),
    ("OUTBOX_BACKLOG", [hb(outbox_pending=501)], []),
    ("LEASE_LOST", [hb(state="EXECUTING", lease_token=None)], []),
    ("DISK_LOW", [hb(disk_free_bytes=1000)], []),
    ("MULTIPLE_WORKERS", [hb(), hb(worker_id="w2", machine_id="m2")], []),
    ("INCIDENT", [hb()], [ev("SOMETHING_ELSE")]),
])
def test_each_finding_type_triggers(code, rows, events):
    assert code in codes(hm.evaluate(rows, events, NOW, {}))


def test_null_lease_in_hedged_and_exiting_is_lease_lost():
    for s in ("HEDGED", "EXITING"):
        assert "LEASE_LOST" in codes(hm.evaluate([hb(state=s)], [], NOW, {}))


def test_lease_changed_in_same_run_is_lease_lost_but_new_run_is_not():
    obs = {}
    hm.evaluate([hb(state="HEDGED", lease_token=1)], [], NOW - 600_000, obs)
    assert "LEASE_LOST" in codes(hm.evaluate([hb(state="HEDGED", lease_token=2)], [], NOW, obs))
    obs = {}
    hm.evaluate([hb(state="HEDGED", lease_token=1)], [], NOW - 600_000, obs)
    assert "LEASE_LOST" not in codes(hm.evaluate([hb(state="HEDGED", lease_token=2, run_id="run-2")], [], NOW, obs))


def test_worker_stuck_needs_unchanged_seq_active_session_and_time():
    obs = {}
    row = hb(state="EXECUTING", lease_token=3, session_id="s1")
    assert "WORKER_STUCK" not in codes(hm.evaluate([row], [], NOW - 1_000_000, obs))
    later = hb(state="EXECUTING", lease_token=3, session_id="s1", at_ms=NOW - 1000)
    assert "WORKER_STUCK" in codes(hm.evaluate([later], [], NOW, obs))
    # progress (seq changed) is not stuck
    obs2 = {}
    hm.evaluate([row], [], NOW - 1_000_000, obs2)
    moved = hb(state="EXECUTING", lease_token=3, session_id="s1", seq=6, at_ms=NOW - 1000)
    assert "WORKER_STUCK" not in codes(hm.evaluate([moved], [], NOW, obs2))
    # no session -> not stuck
    obs3 = {}
    idle = hb()
    hm.evaluate([idle], [], NOW - 1_000_000, obs3)
    assert "WORKER_STUCK" not in codes(hm.evaluate([hb(at_ms=NOW - 1000)], [], NOW, obs3))


def test_unknown_state_and_missing_field_are_unknown_never_healthy():
    assert "UNKNOWN" in codes(hm.evaluate([hb(state="FROBNICATING")], [], NOW, {}))
    row = hb()
    del row["seq"]
    assert "UNKNOWN" in codes(hm.evaluate([row], [], NOW, {}))
    assert "UNKNOWN" in codes(hm.evaluate([hb(outbox_pending="3")], [], NOW, {}))
    assert "UNKNOWN" in codes(hm.evaluate([hb()], [{"kind": "INCIDENT", "payload": "garbage"}], NOW, {}))
    assert "UNKNOWN" in codes(hm.evaluate([hb(at_ms=NOW + 10_000_000)], [], NOW, {}))


def test_stale_heartbeat_suppresses_noise_but_old_workers_do_not_count_as_multiple():
    f = hm.evaluate([hb(at_ms=NOW - 900_000, last_bybit_read_ms=NOW - 900_000), hb(worker_id="w2")], [], NOW, {})
    assert "HEARTBEAT_STALE" in codes(f) and "BYBIT_READ_STALE" not in codes(f)
    assert "MULTIPLE_WORKERS" not in codes(f)


def test_old_incidents_outside_window_are_ignored_and_latest_row_wins():
    old = ev("RISK_LIMIT_X", at_ms=NOW - 10_000_000)
    assert hm.evaluate([hb()], [old], NOW, {}) == []
    rows = [hb(at_ms=NOW - 900_000, state="ALERT"), hb(at_ms=NOW - 1000)]
    assert hm.evaluate(rows, [], NOW, {}) == []


# --------------------------------------------------------------- CLI / failure

class Reader:
    def __init__(self, hbs=None, evs=None, err=None):
        self.hbs, self.evs, self.err = hbs or [], evs or [], err

    def __call__(self, table, query):
        if self.err:
            raise self.err
        return self.hbs if table == "exec_heartbeats" else self.evs


ENV_OK = {"NOTIFY_WEBHOOK_URL": "http://127.0.0.1:1/x"}


def run(tmp_path, reader, env, extra=None, **kw):
    out = io.StringIO()
    args = ["--state-file", str(tmp_path / "s.json"), "--summary-file", str(tmp_path / "sum.md")] + (extra or [])
    rc = hm.cli(args, env, reader=reader, now_ms=NOW, out=out, **kw)
    return rc, out.getvalue()


class Sink(http.server.BaseHTTPRequestHandler):
    posts: list = []
    status = 200
    body = b'{"ok": true}'
    redirect = False

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        Sink.posts.append((self.path, json.loads(self.rfile.read(n))))
        if Sink.redirect:
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/elsewhere")
            self.end_headers()
            return
        self.send_response(Sink.status)
        self.end_headers()
        self.wfile.write(Sink.body)

    def log_message(self, *a):
        pass


@pytest.fixture
def sink():
    Sink.posts, Sink.status, Sink.body, Sink.redirect = [], 200, b'{"ok": true}', False
    srv = http.server.HTTPServer(("127.0.0.1", 0), Sink)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def kw(**more):
    return dict(allow_insecure_loopback_for_tests=True, sleep=lambda s: None, **more)


def test_unreadable_data_is_a_failure_exit_and_notifies(tmp_path, sink):
    rc, out = run(tmp_path, Reader(err=hm.ReadError("read of exec_heartbeats failed (HTTP 500)")),
                  {"NOTIFY_WEBHOOK_URL": sink + "/hook"}, notifier_kwargs=kw())
    assert rc == 2 and "DATA_UNREADABLE" in out
    assert len(Sink.posts) == 1


def test_unexpected_exception_is_also_unreadable(tmp_path):
    rc, _ = run(tmp_path, Reader(err=RuntimeError("boom")), ENV_OK, notifier_kwargs=kw())
    assert rc == 2


def test_missing_supabase_config_fails_closed(tmp_path):
    rc, out = run(tmp_path, None, {}, notifier_kwargs=kw())
    assert rc == 2 and "DATA_UNREADABLE" in out


def test_no_notifier_configured_is_red_even_when_healthy(tmp_path):
    rc, out = run(tmp_path, Reader([hb()]), {})
    assert rc == 1 and "NOTIFIER_NOT_CONFIGURED" in out
    assert "red" in (tmp_path / "sum.md").read_text().lower()


def test_healthy_with_notifier_is_green_and_sends_nothing(tmp_path, sink):
    rc, out = run(tmp_path, Reader([hb()]), {"NOTIFY_WEBHOOK_URL": sink + "/h"}, notifier_kwargs=kw())
    assert rc == 0 and Sink.posts == []
    assert "green" in (tmp_path / "sum.md").read_text()


def test_findings_exit_nonzero_and_notify_once_then_dedupe_then_recover(tmp_path, sink):
    env = {"NOTIFY_WEBHOOK_URL": sink + "/h"}
    bad = Reader([hb(state="ALERT")])
    assert run(tmp_path, bad, env, notifier_kwargs=kw())[0] == 1
    assert len(Sink.posts) == 1 and "ALERT" in Sink.posts[0][1]["text"]
    assert run(tmp_path, bad, env, notifier_kwargs=kw())[0] == 1
    assert len(Sink.posts) == 1                      # de-duplicated
    # after the interval it is re-sent
    out = io.StringIO()
    hm.cli(["--state-file", str(tmp_path / "s.json")], env, reader=bad, now_ms=NOW + 7 * 3600_000,
           out=out, notifier_kwargs=kw())
    assert len(Sink.posts) == 2
    good = Reader([hb(at_ms=NOW + 7 * 3600_000 - 1000, last_bybit_read_ms=NOW + 7 * 3600_000 - 1000,
                      last_market_data_ms=NOW + 7 * 3600_000 - 1000,
                      last_reconcile_ms=NOW + 7 * 3600_000 - 1000,
                      last_supabase_sync_ms=NOW + 7 * 3600_000 - 1000)])
    rc = hm.cli(["--state-file", str(tmp_path / "s.json")], env, reader=good, now_ms=NOW + 7 * 3600_000 + 1,
                out=io.StringIO(), notifier_kwargs=kw())
    assert rc == 0 and len(Sink.posts) == 3 and "RECOVERED" in Sink.posts[2][1]["text"]
    hm.cli(["--state-file", str(tmp_path / "s.json")], env, reader=good, now_ms=NOW + 7 * 3600_000 + 2,
           out=io.StringIO(), notifier_kwargs=kw())
    assert len(Sink.posts) == 3                      # recovery exactly once


def test_unreadable_run_does_not_report_recovery(tmp_path, sink):
    env = {"NOTIFY_WEBHOOK_URL": sink + "/h"}
    run(tmp_path, Reader([hb(state="ALERT")]), env, notifier_kwargs=kw())
    run(tmp_path, Reader(err=hm.ReadError("x")), env, notifier_kwargs=kw())
    assert not any("RECOVERED" in p[1]["text"] for p in Sink.posts)


def test_failed_send_is_retried_next_run_and_goes_red(tmp_path, sink):
    env = {"NOTIFY_WEBHOOK_URL": sink + "/h"}
    Sink.status = 400
    rc, out = run(tmp_path, Reader([hb(state="ALERT")]), env, notifier_kwargs=kw())
    assert rc == 1 and "NOTIFY_FAILED" in out
    Sink.status = 200
    run(tmp_path, Reader([hb(state="ALERT")]), env, notifier_kwargs=kw())
    assert Sink.posts[-1][1]["text"].startswith("HEALTH ALERT")


def test_fail_at_threshold_filters(tmp_path, sink):
    env = {"NOTIFY_WEBHOOK_URL": sink + "/h"}
    rc, _ = run(tmp_path, Reader([hb(state="STOPPED")]), env, ["--fail-at", "ERROR"], notifier_kwargs=kw())
    assert rc == 0


def test_summary_and_output_never_contain_the_keys(tmp_path, sink):
    env = {"NOTIFY_WEBHOOK_URL": sink + "/secret-path-abcdef", "SUPABASE_MONITOR_KEY": KEY,
           "TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": "42"}
    rc, out = run(tmp_path, Reader(err=hm.ReadError("read failed")), env, notifier_kwargs=kw(telegram_api_base=sink))
    blob = out + (tmp_path / "sum.md").read_text() + (tmp_path / "s.json").read_text()
    assert KEY not in blob and TOKEN not in blob and "secret-path-abcdef" not in blob


# ------------------------------------------------------------- supabase reader

def test_supabase_url_validation():
    assert hm.validate_supabase_url("https://abc.supabase.co") == "https://abc.supabase.co"
    for bad in ("http://abc.supabase.co", "https://evil.example.com", "https://abc.supabase.co.evil.com",
                "https://u:p@abc.supabase.co", "https://abc.supabase.co:8443", "https://abc.supabase.co/x", "",
                "https://.supabase.co"):
        with pytest.raises(hm.ReadError):
            hm.validate_supabase_url(bad)


def test_reader_errors_never_contain_key_or_url():
    class Op:
        def open(self, req, timeout):
            raise OSError(f"{req.full_url} {KEY}")
    rd = hm.postgrest_reader("https://abc.supabase.co", KEY, opener=Op())
    with pytest.raises(hm.ReadError) as e:
        rd("exec_heartbeats", "select=*")
    assert KEY not in str(e.value) and "abc.supabase.co" not in str(e.value)
    with pytest.raises(hm.ReadError) as e:
        hm.postgrest_reader("https://abc.supabase.co", "")
    assert KEY not in str(e.value)


def test_reader_is_get_only_with_header_key():
    seen = {}

    class R:
        def read(self, n):
            return b"[]"
        def __enter__(self): return self
        def __exit__(self, *a): return False

    class Op:
        def open(self, req, timeout):
            seen["m"], seen["u"], seen["h"] = req.get_method(), req.full_url, dict(req.header_items())
            return R()
    hm.postgrest_reader("https://abc.supabase.co", KEY, opener=Op())("exec_heartbeats", "select=*")
    assert seen["m"] == "GET" and KEY not in seen["u"] and seen["u"].startswith("https://abc.supabase.co/rest/v1/")
    assert seen["h"]["Accept-profile"] == "carry"


# ---------------------------------------------------------------- notifiers

def test_webhook_posts_json_to_loopback(sink):
    r = nt.WebhookNotifier(sink + "/h", **kw()).send("hello")
    assert r.status == nt.SENT and Sink.posts[0][1]["text"] == "hello"


def test_webhook_refuses_non_https_and_loopback_flag_only_for_127_0_0_1():
    for url in ("http://example.com/x", "http://127.0.0.1:9/x", "ftp://x/y", "https://u:p@example.com/x", "https:///x"):
        with pytest.raises(ValueError):
            nt.WebhookNotifier(url)
    with pytest.raises(ValueError):
        nt.WebhookNotifier("http://localhost:9/x", allow_insecure_loopback_for_tests=True)
    with pytest.raises(ValueError):
        nt.WebhookNotifier("http://10.0.0.1:9/x", allow_insecure_loopback_for_tests=True)
    nt.WebhookNotifier("https://example.com/x")


def test_webhook_refuses_redirects(sink):
    Sink.redirect = True
    r = nt.WebhookNotifier(sink + "/h", **kw()).send("x")
    assert r.status == nt.FAILED and "redirect" in r.detail and len(Sink.posts) == 1


def test_webhook_retries_5xx_boundedly_but_not_4xx(sink):
    Sink.status = 503
    r = nt.WebhookNotifier(sink + "/h", retries=2, **kw()).send("x")
    assert r.status == nt.FAILED and r.attempts == 3
    Sink.status = 404
    Sink.posts.clear()
    r = nt.WebhookNotifier(sink + "/h", retries=2, **kw()).send("x")
    assert r.attempts == 1


def test_webhook_connection_error_is_failed_without_url():
    secret_path = "/hook-SECRETPATH123456"
    r = nt.WebhookNotifier("http://127.0.0.1:1" + secret_path, retries=1, **kw()).send("x")
    assert r.status == nt.FAILED and "SECRETPATH" not in r.detail and r.attempts == 2


def test_telegram_sends_and_token_stays_out_of_everything(sink):
    t = nt.TelegramNotifier(TOKEN, "-100123", api_base=sink, **kw())
    r = t.send("hello " + TOKEN)
    assert r.status == nt.SENT
    path, body = Sink.posts[0]
    assert path == f"/bot{TOKEN}/sendMessage" and body["chat_id"] == "-100123"
    assert TOKEN not in body["text"]
    assert TOKEN not in repr(t) and TOKEN not in str(r) and TOKEN not in r.detail


def test_telegram_not_ok_body_is_failure(sink):
    Sink.body = b'{"ok": false}'
    assert nt.TelegramNotifier(TOKEN, "1", api_base=sink, **kw()).send("x").status == nt.FAILED


def test_telegram_errors_never_leak_token(sink):
    Sink.status = 401
    r = nt.TelegramNotifier(TOKEN, "1", api_base=sink, **kw()).send("x")
    assert TOKEN not in r.detail
    for tok, chat in (("", "1"), (TOKEN, ""), ("bad token!", "1"), (TOKEN, "bad chat!")):
        with pytest.raises(ValueError) as e:
            nt.TelegramNotifier(tok, chat)
        assert TOKEN not in str(e.value)
    with pytest.raises(ValueError):
        nt.TelegramNotifier(TOKEN, "1", api_base="https://evil.example.com")
    with pytest.raises(ValueError):
        nt.TelegramNotifier(TOKEN, "1", api_base="http://127.0.0.1:9")


def test_text_is_length_limited_and_secret_scanned():
    long = "x" * 10000
    assert len(nt.sanitize(long)) <= nt.MAX_TEXT_CHARS
    s = nt.sanitize("ok line\napi_" + "key = AbCdEfGhIjKlMnOpQrStUvWx123456\nbearer " + "a" * 40)
    assert "AbCdEfGh" not in s and "a" * 40 not in s and "ok line" in s
    assert "\x00" not in nt.sanitize("a\x00b")


def test_not_configured_results():
    ns = nt.build_notifiers(None, None, None)
    assert nt.NOT_CONFIGURED == ns[0].send("x").status
    assert isinstance(nt.build_notifiers(None, TOKEN, None)[0], nt.NotConfiguredNotifier)
    assert nt.build_notifiers("https://example.com/x", None, None)[0].name == "webhook"
    out = nt.dispatch(ns, [nt.Alert("k", "CRITICAL", "t")], {}, NOW)
    assert out.not_configured and out.alerts_sent == []


def test_state_roundtrip_and_corruption(tmp_path):
    p = str(tmp_path / "st.json")
    assert nt.load_state(p) == {"version": 1, "active": {}}
    nt.save_state(p, {"active": {"a": {"last_notified_ms": 1}}})
    assert nt.load_state(p)["active"]["a"]["last_notified_ms"] == 1
    open(p, "w").write("{not json")
    assert nt.load_state(p)["active"] == {}


# ----------------------------------------------------------------- workflow

@pytest.fixture(scope="module")
def wf():
    return open(WF, encoding="utf-8").read()


def test_workflow_is_off_by_default_main_only(wf):
    assert ("github.repository == 'iceccarelli/ai-agent-control' && github.ref == 'refs/heads/main' "
            "&& vars.FLY_MONITOR_ENABLED == 'true'") in wf
    assert re.search(r"^\s+if: .*FLY_MONITOR_ENABLED == 'true'", wf, re.M)


def test_workflow_triggers_and_hardening(wf):
    assert re.search(r"^on:\n  schedule:\n    - cron: \"\*/10 \* \* \* \*\"\n  workflow_dispatch:\n", wf, re.M)
    code = "\n".join(l for l in wf.splitlines() if not l.lstrip().startswith("#"))
    assert "pull_request" not in code
    assert re.search(r"^permissions:\n  contents: read\n", wf, re.M)
    assert re.search(r"^concurrency:\n  group: ", wf, re.M) and "timeout-minutes:" in wf
    for ref in re.findall(r"^\s*-?\s*uses:\s*(\S+)", wf, re.M):
        assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", ref), ref
    assert "runs-on: ubuntu-latest" in wf


def test_workflow_has_no_exchange_names_or_endpoints(wf):
    low = wf.lower()
    for bad in ("bybit_", "bybit", "live_", "api.bybit", "api_secret", "fly_api_token", "service_role"):
        assert bad not in low, bad
    used = set(re.findall(r"secrets\.([A-Z_]+)", wf))
    assert used <= {"SUPABASE_URL", "SUPABASE_MONITOR_KEY", "NOTIFY_WEBHOOK_URL", "TELEGRAM_BOT_TOKEN",
                    "TELEGRAM_CHAT_ID"}, used


def test_monitor_and_notifier_sources_never_touch_the_exchange():
    for name in ("health_monitor.py", "notifier.py"):
        src = open(os.path.join(os.path.dirname(HERE), "tools", name), encoding="utf-8").read().lower()
        assert "api.bybit" not in src and "bybit_api" not in src and "live_trading" not in src
        assert "import bybit" not in src and "pybit" not in src
