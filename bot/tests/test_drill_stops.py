"""The drill's own stops: one-way mode only, a drill-owned kill file, and an armed-entry limit that holds ACROSS processes.
Reproduced before the fix: a second drill process on the same UTC day got a fresh allowance; the scratch-database kill switch could never be engaged;
a hedge-mode account was not noticed until the venue rejected the order."""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, BOT)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(BOT, "tools"))

import drill as D  # noqa: E402
from test_drill import Venue  # noqa: E402

DAY = 1_700_000_000_000
NEXT_DAY = DAY + 86_400_000


def run(venue, **kw):
    kw.setdefault("arm", True)
    kw.setdefault("notional", 100.0)
    return D.run_drill(broker=venue, **kw)


# ---------------------------------------------------------------- position mode

def test_a_hedge_mode_account_is_refused_before_any_order_armed_or_not():
    for arm in (True, False):
        v = Venue(); v.position_mode = "hedge"
        rep = run(v, arm=arm)
        assert rep["verdict"] == "FAILED" and rep["failed_stage"] == "position_mode" and rep["orders_sent"] == 0 and v.orders == []


def test_one_way_mode_passes_positive_control():
    assert run(Venue())["verdict"] == "PASSED"


def test_a_broker_that_cannot_read_the_mode_is_refused_when_armed_but_not_for_a_preflight():
    class NoMode(Venue):
        get_position_mode = None
    v = NoMode()
    assert run(v)["failed_stage"] == "position_mode" and v.orders == []
    assert run(NoMode(), arm=False)["verdict"] == "PREFLIGHT_ONLY"


# ---------------------------------------------------------------- kill file

def test_kill_file_engaged_semantics(tmp_path):
    assert D.kill_file_engaged("") == (False, "")
    assert D.kill_file_engaged(str(tmp_path / "absent"))[0] is False
    f = tmp_path / "KILL"; f.write_text("")
    assert D.kill_file_engaged(str(f))[0] is True
    d = tmp_path / "KILLDIR"; d.mkdir()
    assert D.kill_file_engaged(str(d))[0] is True
    l = tmp_path / "KILLLINK"; os.symlink(str(tmp_path / "nowhere"), l)             # a dangling link still counts: it exists
    assert D.kill_file_engaged(str(l))[0] is True


def test_an_armed_drill_with_the_kill_file_present_sends_nothing(tmp_path):
    k = tmp_path / "DRILL_KILL"; k.write_text("")
    v = Venue()
    rep = run(v, kill_file=str(k))
    assert rep["verdict"] == "FAILED" and rep["failed_stage"] == "kill_switch" and v.orders == []
    k.unlink()
    assert run(Venue(), kill_file=str(k))["verdict"] == "PASSED"                     # control: absent file does not block


# ---------------------------------------------------------------- entry limit

def test_the_second_armed_drill_of_a_utc_day_is_refused_even_from_another_process(tmp_path):
    ledger = str(tmp_path / "entries.jsonl")
    assert D.claim_entry_slot(ledger, DAY)[0] is True
    code = ("import sys; sys.path[:0]=[%r,%r]; import drill as D; g,w=D.claim_entry_slot(%r,%d); print(g,w)" % (BOT, os.path.join(BOT, "tools"), ledger, DAY + 1000))
    out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60)
    assert out.stdout.startswith("False") and "ENTRY_LIMIT_REACHED_TODAY" in out.stdout, out.stdout + out.stderr


def test_a_new_utc_day_gets_a_new_allowance(tmp_path):
    ledger = str(tmp_path / "entries.jsonl")
    assert D.claim_entry_slot(ledger, DAY)[0] and not D.claim_entry_slot(ledger, DAY + 5)[0]
    assert D.claim_entry_slot(ledger, NEXT_DAY)[0]


def test_concurrent_claims_grant_exactly_one(tmp_path):
    """Many processes released at the same instant (they spin on a start file) race for one slot; the lock makes exactly one win."""
    ledger = str(tmp_path / "entries.jsonl")
    go = str(tmp_path / "go")
    code = ("import sys,os,time; sys.path[:0]=[%r,%r]; import drill as D\n"
            "while not os.path.exists(%r): pass\n"
            "print(D.claim_entry_slot(%r,%d)[0])" % (BOT, os.path.join(BOT, "tools"), go, ledger, DAY))
    procs = [subprocess.Popen([sys.executable, "-I", "-c", code], stdout=subprocess.PIPE, text=True) for _ in range(24)]
    import time
    time.sleep(3.0)                       # let every child finish importing and reach the spin
    open(go, "w").close()
    outs = [p.communicate(timeout=120)[0].strip() for p in procs]
    assert outs.count("True") == 1 and outs.count("False") == 23, outs


def test_an_unreadable_ledger_row_refuses_rather_than_guessing(tmp_path):
    ledger = tmp_path / "entries.jsonl"; ledger.write_text("not json\n")
    granted, why = D.claim_entry_slot(str(ledger), DAY)
    assert granted is False and "unreadable row" in why


@pytest.mark.real_entry_ledger
def test_no_ledger_named_refuses_an_armed_drill_and_sends_nothing():
    v = Venue()
    rep = run(v)
    assert rep["verdict"] == "FAILED" and rep["failed_stage"] == "entry_limit" and v.orders == []


def test_two_armed_runs_on_one_ledger_the_second_sends_nothing(tmp_path):
    ledger = str(tmp_path / "entries.jsonl")
    assert run(Venue(), entry_ledger=ledger)["verdict"] == "PASSED"
    v2 = Venue()
    rep = run(v2, entry_ledger=ledger)
    assert rep["failed_stage"] == "entry_limit" and v2.orders == []


def test_a_preflight_does_not_spend_the_days_slot(tmp_path):
    ledger = str(tmp_path / "entries.jsonl")
    assert run(Venue(), arm=False, entry_ledger=ledger)["verdict"] == "PREFLIGHT_ONLY"
    assert not os.path.exists(ledger) or os.path.getsize(ledger) == 0
    assert run(Venue(), entry_ledger=ledger)["verdict"] == "PASSED"


def test_the_claim_takes_an_exclusive_lock_before_it_reads_the_ledger(tmp_path, monkeypatch):
    """Deterministic companion to the race test above (a race that mostly resolves in the right order cannot prove the lock): the exclusive flock must be requested."""
    import fcntl
    calls = []
    real = fcntl.flock
    monkeypatch.setattr(fcntl, "flock", lambda fd, op: (calls.append(op), real(fd, op))[1])
    assert D.claim_entry_slot(str(tmp_path / "e.jsonl"), DAY)[0] is True
    assert calls and calls[0] == fcntl.LOCK_EX
