"""The safety-net close: recording is observable, bounded and never in the way.

Defect 1: `_recording_broker` built its `RecordWriter` inline and dropped it, so
the writer's error counter was unreachable. A close that executed but whose
record could not be written reported nothing.

Defect 2: the optional pre-close reads were not bounded. `broker.get_mark` takes
no timeout and `_request` retries; `LedgerBroker._read_touch` called a getter
that cannot take a timeout "as it is". A hung market-data read could hold an
emergency close back. Passing `timeout_s` as an argument is not a bound.

Everything is offline: fake adapters, temporary files, no network.
"""
from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
for p in (BOT, os.path.join(BOT, "tools"), HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import close_testnet_short as cts                             # noqa: E402
import drill                                                  # noqa: E402
import ledger as L                                            # noqa: E402

QTY = 0.001


class Venue:
    """Fills a reduce-only Buy at a settable price; counts every order."""
    VENUE = "testnet"
    px = 100_000.0

    def __init__(self, short=QTY, reject=False):
        self.short, self.reject, self.sent = short, reject, []
        self.at_order = []                      # timer state when the order left

    def get_mark(self, s): return self.px
    def get_spot_mark(self, s): return self.px
    def get_perp_position(self, s): return float(self.short)
    def get_open_carry_orders(self, a, b): return []
    def get_position_slots(self, s): return []

    def get_book_top(self, symbol, product, *, timeout_s=None):
        return {"bid": self.px - 5, "ask": self.px + 5}

    def place_market(self, *, symbol, side, qty, product):
        self.at_order.append(signal.getitimer(signal.ITIMER_REAL))
        self.sent.append((side, qty, product))
        if self.reject:
            return None
        if side == "Buy" and product == "linear":
            self.short = max(0.0, self.short - qty)
        return {"filled_qty": qty, "avg_price": self.px,
                "order_link_id": f"L{len(self.sent)}", "fee": 0.2,
                "fee_status": "confirmed"}


def close(venue, path, **kw):
    return cts.close_short(venue, "BTCUSDT", expected_qty=QTY,
                           sleep=lambda s: None, records_path=path,
                           venue_label="testnet", **kw)


# -- Defect 1: the writer's final status is kept and exposed -------------------

def test_baseline_a_successful_close_reports_its_recording_as_ok(tmp_path):
    out = close(Venue(), str(tmp_path / "r.jsonl"))
    assert out["ok"] and out["recording"]["state"] == "OK"
    assert out["recording"]["records_written"] >= 1
    assert out["recording"]["writer_errors"] == 0


def test_an_unwritable_path_is_reported_and_does_not_fail_the_close(tmp_path):
    v = Venue()
    out = close(v, str(tmp_path))                  # a directory: cannot append
    assert out["ok"] is True and out["flat_verified"] is True
    assert out["recording"]["state"] == "FAILED"
    assert out["recording"]["writer_errors"] >= 1
    assert out["recording"]["writer_last_error"]
    assert v.sent == [("Buy", QTY, "linear")]      # one order, not two


def test_a_write_failure_is_reported(tmp_path, monkeypatch):
    path = str(tmp_path / "r.jsonl")

    def boom(fd, data):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(os, "write", boom)
    v = Venue()
    out = close(v, path)
    assert out["ok"] and out["recording"]["state"] == "FAILED"
    assert "No space" in out["recording"]["writer_last_error"]
    assert len(v.sent) == 1


def test_an_fsync_failure_is_reported_as_unsynced_not_as_lost(tmp_path,
                                                              monkeypatch):
    path = str(tmp_path / "r.jsonl")

    def boom(fd):
        raise OSError(5, "Input/output error")
    monkeypatch.setattr(os, "fsync", boom)
    v = Venue()
    out = close(v, path)
    assert out["ok"] and len(v.sent) == 1
    # the line reached the file but its durability is NOT confirmed: say so
    assert out["recording"]["state"] == "UNSYNCED"
    assert "Input/output" in out["recording"]["writer_last_error"]
    assert len(open(path).read().splitlines()) == 1


def test_a_transient_write_failure_is_recovered_by_the_flush(tmp_path,
                                                             monkeypatch):
    path = str(tmp_path / "r.jsonl")
    real = os.write
    calls = {"n": 0}

    def flaky(fd, data):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(5, "transient")
        return real(fd, data)
    monkeypatch.setattr(os, "write", flaky)
    v = Venue()
    out = close(v, path)
    assert out["ok"] and len(v.sent) == 1          # still ONE order
    assert out["recording"]["state"] == "RECOVERED"
    assert out["recording"]["writer_errors"] == 1
    assert len(open(path).read().splitlines()) == 1


def test_a_rejected_close_still_reports_its_recording(tmp_path):
    v = Venue(reject=True)
    out = close(v, str(tmp_path))                  # unwritable AND rejected
    assert out["ok"] is False and out["sent"] is False
    assert out["recording"]["state"] == "FAILED"
    assert len(v.sent) == 1


def test_a_rejected_close_records_its_outcome_when_it_can(tmp_path):
    v = Venue(reject=True)
    path = str(tmp_path / "r.jsonl")
    out = close(v, path)
    assert out["ok"] is False and out["recording"]["state"] == "OK"
    (line,) = [json.loads(x) for x in open(path).read().splitlines()]
    assert line["kind"] == drill.OUTCOME_KIND


def test_a_refused_close_says_no_recording_was_attempted(tmp_path):
    v = Venue(short=QTY)
    out = cts.close_short(v, "BTCUSDT", expected_qty=0.5, sleep=lambda s: None,
                          records_path=str(tmp_path / "r.jsonl"))
    assert out["ok"] is False and "refused" in out
    assert out["recording"]["state"] == "NOT_ATTEMPTED"
    assert v.sent == []


def test_an_already_flat_venue_says_no_recording_was_attempted(tmp_path):
    v = Venue(short=0.0)
    out = cts.close_short(v, "BTCUSDT", expected_qty=0.0, sleep=lambda s: None,
                          records_path=str(tmp_path / "r.jsonl"))
    assert out["ok"] and out["sent"] is False
    assert out["recording"]["state"] == "NOT_ATTEMPTED"


def test_no_records_path_is_not_configured_and_changes_nothing():
    v = Venue()
    out = cts.close_short(v, "BTCUSDT", expected_qty=QTY, sleep=lambda s: None)
    assert out["ok"] and out["recording"]["state"] == "NOT_CONFIGURED"
    assert v.sent == [("Buy", QTY, "linear")]


def test_setup_failure_is_reported_and_the_close_still_goes_out(tmp_path,
                                                                monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("cannot build the recorder")
    monkeypatch.setattr(L, "LedgerBroker", boom)
    v = Venue()
    out = close(v, str(tmp_path / "r.jsonl"))
    assert out["ok"] and out["recording"]["state"] == "SETUP_FAILED"
    assert "cannot build" in out["recording"]["setup_error"]
    assert v.sent == [("Buy", QTY, "linear")]


def test_the_status_file_carries_the_recording_state(tmp_path):
    status = tmp_path / "status.json"
    v = Venue()
    out = close(v, str(tmp_path))                      # unwritable
    cts.write_recording_status(str(status), out)
    data = json.loads(status.read_text())
    assert data["recording"]["state"] == "FAILED"
    assert data["flat_verified"] is True


def test_the_unattended_verdict_surfaces_a_close_recording_problem(tmp_path):
    import unattended_drill as ud
    status = tmp_path / "close_recording.json"
    status.write_text(json.dumps({"recording": {
        "state": "FAILED", "writer_last_error": "OSError: disk full"},
        "flat_verified": True}))
    msg = ud.close_recording_problem(str(status))
    assert msg and "disk full" in msg
    status.write_text(json.dumps({"recording": {"state": "OK"}}))
    assert ud.close_recording_problem(str(status)) is None
    assert ud.close_recording_problem(str(tmp_path / "absent.json")) is None


# -- Defect 2: no unbounded read in front of an emergency close ---------------

class BlockingNoTimeout(Venue):
    """Market-data getters that cannot take a timeout and would hang."""
    def __init__(self, **kw):
        super().__init__(**kw)
        self.calls = []

    def get_mark(self, s):
        self.calls.append("get_mark")
        raise AssertionError("get_mark must not be read before the close")

    def get_book_top(self, symbol, product):               # no timeout_s
        self.calls.append("get_book_top")
        raise AssertionError("an unbounded getter was called before the close")

    def get_book_depth(self, symbol, product):             # no timeout_s
        self.calls.append("get_book_depth")
        raise AssertionError("an unbounded getter was called before the close")


def test_a_getter_that_cannot_take_a_timeout_is_never_called(tmp_path):
    v = BlockingNoTimeout()
    path = str(tmp_path / "r.jsonl")
    out = close(v, path)
    assert out["ok"] and out["flat_verified"] and len(v.sent) == 1
    assert v.calls == []                      # proven: it was not called at all
    (line,) = [json.loads(x) for x in open(path).read().splitlines()]
    ex = line["fee_raw"]["execution"]
    assert ex["status"] != "MEASURED"         # unmeasured, not invented
    assert ex.get("bid") in (None, 0) or ex.get("reference_basis") != "executable_side"


class SlowIgnoresTimeout(Venue):
    """Accepts timeout_s but ignores it and blocks (a hung socket / DNS)."""
    def __init__(self, **kw):
        super().__init__(**kw)
        self.calls = 0

    def get_book_top(self, symbol, product, *, timeout_s=None):
        self.calls += 1
        time.sleep(30)
        return {"bid": 1.0, "ask": 2.0}


def test_a_hung_getter_that_ignores_its_timeout_cannot_hold_the_close(tmp_path):
    v = SlowIgnoresTimeout()
    path = str(tmp_path / "r.jsonl")
    before = threading.active_count()
    t0 = time.monotonic()
    out = close(v, path)
    took = time.monotonic() - t0
    assert out["ok"] and len(v.sent) == 1
    assert took < 5.0                                  # not 30 s
    assert v.calls == 1
    assert threading.active_count() == before          # no hidden work left
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)   # timer disarmed
    ex = json.loads(open(path).read().splitlines()[0])["fee_raw"]["execution"]
    assert ex["status"] != "MEASURED"


def test_the_deadline_is_disarmed_before_the_order_leaves(tmp_path):
    v = Venue()
    close(v, str(tmp_path / "r.jsonl"))
    assert v.at_order == [(0.0, 0.0)]


def test_a_bounded_adapter_with_a_valid_book_is_measured(tmp_path):
    v = Venue()
    path = str(tmp_path / "r.jsonl")
    out = close(v, path)
    assert out["ok"] and out["recording"]["state"] == "OK"
    ex = json.loads(open(path).read().splitlines()[0])["fee_raw"]["execution"]
    assert ex["status"] == "MEASURED" and ex["reference_basis"] == "executable_side"


def test_no_hard_deadline_available_means_no_optional_read(tmp_path):
    """Off the main thread a signal timer cannot be armed; the optional read is
    then skipped rather than made unbounded."""
    v = BlockingNoTimeout()
    v.get_book_top = lambda symbol, product, *, timeout_s=None: (
        v.calls.append("get_book_top") or {"bid": 1.0, "ask": 2.0})
    result = {}

    def run():
        result["out"] = close(v, str(tmp_path / "r.jsonl"))
    t = threading.Thread(target=run)
    t.start()
    t.join(10)
    assert result["out"]["ok"] and len(v.sent) == 1
    assert v.calls == []


def test_get_mark_is_not_read_before_the_close(tmp_path):
    v = Venue()
    calls = []                       # recorded, not raised: a raise could be
    v.get_mark = lambda s: calls.append("get_mark") or 1.0   # swallowed upstream
    out = close(v, str(tmp_path / "r.jsonl"))
    assert out["ok"] and len(v.sent) == 1
    assert calls == []


def test_read_touch_never_calls_a_getter_that_cannot_take_a_timeout():
    class Inner:
        VENUE = "testnet"
        called = False

        def get_book_top(self, symbol, product):
            Inner.called = True
            return {"bid": 1.0, "ask": 2.0}
    b = L.LedgerBroker(Inner(), L.Journal(), ms=lambda: 1_700_000_000_000)
    assert b._read_touch("BTCUSDT", "linear", timeout_s=1.0) is None
    assert Inner.called is False
    # with no bound requested (an entry may wait) the old behaviour stands
    assert b._read_touch("BTCUSDT", "linear", timeout_s=None) is not None


# -- the raised-order path and a second hanging source ------------------------------

def test_an_order_that_raises_still_carries_the_recording_status(tmp_path):
    class Raises(Venue):
        def place_market(self, **kw):
            self.sent.append("attempt")
            raise RuntimeError("ambiguous: the order may or may not be live")
    v = Raises()
    with pytest.raises(RuntimeError) as err:
        close(v, str(tmp_path))                      # unwritable AND raising
    assert err.value.recording["state"] in ("FAILED", "NOT_WRITTEN")
    assert v.sent == ["attempt"]                    # one attempt, no resend


def test_two_hanging_sources_cannot_chain_their_waits(tmp_path):
    """If the deadline were an ordinary Exception, `_read_touch` would swallow
    it on the first source and wait on the second: 2 x the hang."""
    class TwoHangs(Venue):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.calls = []

        def get_book_depth(self, symbol, product, *, timeout_s=None):
            self.calls.append("depth")
            time.sleep(30)

        def get_book_top(self, symbol, product, *, timeout_s=None):
            self.calls.append("top")
            time.sleep(30)
    v = TwoHangs()
    t0 = time.monotonic()
    out = close(v, str(tmp_path / "r.jsonl"))
    assert out["ok"] and len(v.sent) == 1
    assert time.monotonic() - t0 < 6.0
    assert v.calls == ["depth"]                      # the second never started


def _tool_help(tool):
    import subprocess
    r = subprocess.run([sys.executable, os.path.join(BOT, "tools", tool),
                        "--help"], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_every_flag_the_runner_passes_is_a_flag_its_tool_accepts(tmp_path):
    """The real `run_once`, with a capturing runner: each command's flags are
    checked against the --help of the tool it is sent to. (A flag handed to the
    wrong tool is an argparse error, i.e. a crashed drill.)"""
    from test_unattended_drill import Recorder, run
    rec = Recorder()
    run(tmp_path, rec)
    seen = {}
    for tool, argv, _env, _t in rec.calls:
        seen[tool] = argv
    assert "drill.py" in seen and "close_testnet_short.py" in seen
    for tool in ("drill.py", "close_testnet_short.py"):
        help_text = _tool_help(tool)
        for token in seen[tool]:
            if isinstance(token, str) and token.startswith("--"):
                assert token in help_text, f"{tool} does not accept {token}"
    # and they went to the right tool
    assert "--recording-status-out" in seen["close_testnet_short.py"]
    assert "--recording-status-out" not in seen["drill.py"]
    assert "--execution-records" in seen["drill.py"]
    assert "--execution-records" in seen["close_testnet_short.py"]


def test_a_getter_that_swallows_exceptions_in_a_retry_loop_is_still_cut_off(tmp_path):
    """Adapters wrap their waits in retry loops that `except Exception`. The
    deadline must not be an Exception, or the one-shot alarm is swallowed and
    the loop carries on waiting."""
    class Retrying(Venue):
        def get_book_top(self, symbol, product, *, timeout_s=None):
            for _ in range(6):
                try:
                    time.sleep(1.5)
                except Exception:
                    pass                      # a naive retry-on-any-error loop
            return {"bid": 1.0, "ask": 2.0}
    v = Retrying()
    t0 = time.monotonic()
    out = close(v, str(tmp_path / "r.jsonl"))
    assert out["ok"] and len(v.sent) == 1
    assert time.monotonic() - t0 < 4.5        # cut at ~2 s, not after 9 s
