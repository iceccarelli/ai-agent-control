"""The owner's halt-clearing commands need the OWNER (red-team F2, HIGH).

Before: ``capital --ack-halt | --ack-drawdown | --ack-review | --reserve-fraction | --declare-transfer``, ``clear-alert`` and ``resume`` changed risk state for any process that could run the CLI. A real
subprocess with stdin=/dev/null cleared a four-loss stop (rc 0); ``--ack-halt`` left no trace anywhere. Only ``grant`` demanded an interactive terminal and a typed reference.

After: each of them (``resume`` only for the PAUSED / STOP markers, which are the owner's own stops: DRAIN / DRAINED are the deploy's and stay scriptable) needs an interactive terminal AND a typed line
that embeds the CURRENT machine facts (streak, realised result, last session ...), so a line typed for an old state or by a script that never read the state does not work. A refusal changes nothing
and says so; every attempt, refusal and success appends an fsynced row to ``marker_log.jsonl`` (before/after state, tty, uid), and the row announcing an action is written BEFORE the action, so a log
that cannot be written means nothing is cleared.
"""
from __future__ import annotations

import json
import os
import re
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
for _p in (HERE, BOT, os.path.join(BOT, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import capital_testkit as CT  # noqa: E402
import ms_capital as CC  # noqa: E402
import ms_engine_state as ES  # noqa: E402
import ms_testnet_autopilot as AP  # noqa: E402

CLOCK = lambda: 1_794_312_000_000  # noqa: E731


def _halted_book(root: str, losses: int = 4, account: str = CT.ACCOUNT) -> CC.CapitalBook:
    b = CT.anchored(root, account)
    b.ensure_baseline("10000")
    cash = 10_000
    for i in range(losses):
        assert b.park(f"loss_{i}", str(cash), "-1.00", True, True)
        cash -= 1
        b.settle(str(cash))
    assert b.load()["halted"] is True and b.load()["loss_streak"] == losses
    return b


def _run(argv, *, tty, typed=None, uid=4242, capsys=None, env=None):
    """main() with an injected terminal. ``typed`` may be a string or a callable that receives the printed prompt (so a test can type what the screen asked for)."""
    def input_fn(prompt):
        return typed(capsys.readouterr().out) if callable(typed) else (typed or "")
    rc = AP.main(list(argv), env=env or {}, isatty=lambda: tty, input_fn=input_fn, uid=uid)
    return rc, (capsys.readouterr().out if capsys is not None else "")


def _line_asked_for(out: str) -> str:
    m = re.search(r"^(CONFIRM .+)$", out, flags=re.M)
    assert m, out
    return m.group(1).strip()


def D_eq(a, b):
    from decimal import Decimal
    return Decimal(str(a)) == Decimal(str(b))


def _log_rows(root: str):
    p = os.path.join(root, "marker_log.jsonl")
    return [json.loads(l) for l in open(p, encoding="utf-8")] if os.path.exists(p) else []


# ------------------------------------------------------------------------------------------------------------------------------------------------------------------ the capital acknowledgements
def test_ack_halt_without_a_terminal_is_refused_changes_nothing_and_leaves_a_trace(tmp_path, capsys):
    root = str(tmp_path)
    _halted_book(root)
    before = CT.anchored(root).load()
    rc, out = _run(["capital", "--root", root, "--ack-halt"], tty=False, capsys=capsys)
    assert rc == 2 and "owner-only" in out and "nothing was changed" in out, out
    assert CT.anchored(root).load() == before and before["halted"] is True
    rows = _log_rows(root)
    assert [r["action"] for r in rows] == ["REFUSED_NO_TTY"] and rows[0]["payload"]["tty"] is False and rows[0]["payload"]["uid"] == 4242 and rows[0]["marker"] == "CAPITAL_BOOK"


def test_ack_halt_with_a_terminal_and_the_line_that_embeds_the_current_state_succeeds_and_is_audited(tmp_path, capsys):
    root = str(tmp_path)
    _halted_book(root)
    rc, out = _run(["capital", "--root", root, "--ack-halt"], tty=True, typed=_line_asked_for, capsys=capsys)
    assert rc == 0 and "halt acknowledged" in out, out
    b = CT.anchored(root).load()
    assert b["halted"] is False and b["loss_streak"] == 0
    assert b["halt_acknowledged"][-1]["loss_streak"] == 4 and D_eq(b["halt_acknowledged"][-1]["realised_usd"], "-4"), "the book itself remembers that a stop was acknowledged, and what it was"
    rows = _log_rows(root)
    assert [r["action"] for r in rows] == ["INTENT", "APPLIED"] and all(r["payload"]["tty"] is True and r["payload"]["uid"] == 4242 for r in rows)
    assert rows[0]["payload"]["before"]["halted"] is True and rows[1]["payload"]["after"]["halted"] is False and rows[1]["payload"]["after"]["loss_streak"] == 0
    assert "loss_streak=4" in rows[0]["payload"]["line"] and "last_session=loss_3" in rows[0]["payload"]["line"]


def test_the_prompt_names_the_facts_being_acknowledged(tmp_path, capsys):
    root = str(tmp_path)
    _halted_book(root)
    rc, out = _run(["capital", "--root", root, "--ack-halt"], tty=True, typed="", capsys=capsys)
    assert rc == 2
    line = _line_asked_for(out)
    assert "ACK-HALT" in line and "loss_streak=4" in line and "realised_usd=-4" in line and "last_session=loss_3" in line, line


def test_a_line_typed_for_an_older_state_is_refused(tmp_path, capsys):
    """The line embeds the live facts. A script that read the state yesterday (or that never read it) cannot answer today's prompt."""
    root = str(tmp_path)
    b = _halted_book(root)
    rc, out = _run(["capital", "--root", root, "--ack-halt"], tty=True, typed="", capsys=capsys)
    old_line = _line_asked_for(out)
    assert b.park("loss_4", "9996", "-1.00", True, True) and b.settle("9995")["status"]            # the state moved on: streak 5, another loss booked
    rc, out = _run(["capital", "--root", root, "--ack-halt"], tty=True, typed=old_line, capsys=capsys)
    assert rc == 2 and "does not match" in out and CT.anchored(root).load()["halted"] is True
    assert [r["action"] for r in _log_rows(root)][-1] == "REFUSED_MISMATCH"


@pytest.mark.parametrize("typed", ["", "yes", "CONFIRM ACK-HALT", "confirm ack-halt loss_streak=4"])
def test_any_other_typed_text_is_refused(tmp_path, capsys, typed):
    root = str(tmp_path)
    _halted_book(root)
    rc, out = _run(["capital", "--root", root, "--ack-halt"], tty=True, typed=typed, capsys=capsys)
    assert rc == 2 and CT.anchored(root).load()["halted"] is True


@pytest.mark.parametrize("flag,args,setup", [
    ("--ack-drawdown", [], "drawdown"),
    ("--ack-review", [], "review"),
    ("--reserve-fraction", ["0.1"], "plain"),
    ("--declare-transfer", ["wd-1", "-10"], "plain"),
])
def test_every_capital_mutation_needs_the_owner_and_is_audited(tmp_path, capsys, flag, args, setup):
    root = str(tmp_path)
    b = CT.anchored(root)
    b.ensure_baseline("10000")
    if setup == "drawdown":
        assert b.park("s1", "10000", "-1.00", True, True) and b.settle("9999")["status"]               # the peak (10000) is above today's basis (9999): there is a drawdown to acknowledge
    elif setup == "review":
        assert b.park("s1", "10000", "0.50", True, True)
        assert b.settle("20000")["status"] == "UNEXPLAINED_CASH"
    before = CT.anchored(root).load()
    argv = ["capital", "--root", root, flag, *args]
    rc, out = _run(argv, tty=False, capsys=capsys)
    assert rc == 2 and CT.anchored(root).load() == before, out
    rc, out = _run(argv, tty=True, typed="not the line", capsys=capsys)
    assert rc == 2 and CT.anchored(root).load() == before, out
    rc, out = _run(argv, tty=True, typed=_line_asked_for, capsys=capsys)
    assert rc == 0, out
    assert CT.anchored(root).load() != before, "the confirmed action must have been applied"
    actions = [r["action"] for r in _log_rows(root)]
    assert actions == ["REFUSED_NO_TTY", "REFUSED_MISMATCH", "INTENT", "APPLIED"], actions


def test_printing_the_book_needs_no_terminal_and_writes_nothing(tmp_path, capsys):
    root = str(tmp_path)
    _halted_book(root)
    before = CT.anchored(root).load()
    rc, out = _run(["capital", "--root", root], tty=False, capsys=capsys)
    assert rc == 0 and '"halted": true' in out and CT.anchored(root).load() == before and _log_rows(root) == []


def test_nothing_is_cleared_when_the_audit_row_cannot_be_written(tmp_path, capsys, monkeypatch):
    root = str(tmp_path)
    _halted_book(root)

    def broken(self, name, action, payload):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(ES.EngineState, "audit", broken)
    rc, out = _run(["capital", "--root", root, "--ack-halt"], tty=True, typed=_line_asked_for, capsys=capsys)
    assert rc == 1 and "audit log could not be written" in out and "nothing was changed" in out, out
    assert CT.anchored(root).load()["halted"] is True


def test_acknowledging_nothing_is_still_a_confirmed_and_audited_action_that_reports_nothing_to_do(tmp_path, capsys):
    root = str(tmp_path)
    b = CT.anchored(root)
    b.ensure_baseline("10000")
    rc, out = _run(["capital", "--root", root, "--ack-halt"], tty=True, typed=_line_asked_for, capsys=capsys)
    assert rc == 0 and "no halt to acknowledge" in out
    assert [r["action"] for r in _log_rows(root)] == ["INTENT", "NOOP"]


# ------------------------------------------------------------------------------------------------------------------------------------------------------------------ clear-alert and resume
def test_clear_alert_needs_the_owner_before_anything_is_asked_of_the_venue(tmp_path, capsys, monkeypatch):
    root = str(tmp_path)
    eng = ES.EngineState(root, CLOCK)
    eng.set("ALERT", {"code": "ACCOUNT_CASH_UNEXPLAINED"})
    called = []
    monkeypatch.setattr(AP, "cmd_clear_alert", lambda *a, **k: called.append((a, k)) or 0)
    rc, out = _run(["clear-alert", "--root", root, "--reason", "looked"], tty=False, capsys=capsys)
    assert rc == 2 and called == [] and eng.has("ALERT"), out
    rc, out = _run(["clear-alert", "--root", root, "--reason", "looked"], tty=True, typed=_line_asked_for, capsys=capsys)
    assert rc == 0 and len(called) == 1
    assert "alert_code=ACCOUNT_CASH_UNEXPLAINED" in [r for r in _log_rows(root) if r["action"] == "INTENT"][-1]["payload"]["line"]


def test_resume_clears_the_deploys_markers_without_a_terminal_but_not_the_owners_own_stops(tmp_path, capsys):
    root = str(tmp_path)
    eng = ES.EngineState(root, CLOCK)
    for m in ("DRAIN", "DRAINED"):
        eng.set(m, {"at_ms": CLOCK()})
    rc, out = _run(["resume", "--root", root, "--reason", "deployed abc after a verified drain"], tty=False, capsys=capsys)
    assert rc == 0 and not eng.has("DRAIN") and not eng.has("DRAINED"), out                          # the scripted deploy (safe-deploy.sh) keeps working
    for m in ("DRAIN", "DRAINED", "STOP"):
        eng.set(m, {"at_ms": CLOCK()})
    rc, out = _run(["resume", "--root", root, "--reason", "deployed abc after a verified drain"], tty=False, capsys=capsys)
    assert rc == 2 and eng.has("STOP") and not eng.has("DRAIN") and not eng.has("DRAINED"), out
    assert "STOP" in out and "owner-only" in out and "left in place" in out
    eng.set("PAUSED", {"at_ms": CLOCK()})
    rc, out = _run(["resume", "--root", root, "--reason", "owner decided"], tty=True, typed=_line_asked_for, capsys=capsys)
    assert rc == 0 and not eng.has("STOP") and not eng.has("PAUSED"), out
    assert "markers=PAUSED,STOP" in [r for r in _log_rows(root) if r["action"] == "INTENT"][-1]["payload"]["line"]


def test_resume_with_an_open_alert_is_still_refused_whoever_asks(tmp_path, capsys):
    root = str(tmp_path)
    eng = ES.EngineState(root, CLOCK)
    eng.set("ALERT", {"code": "X"})
    rc, out = _run(["resume", "--root", root, "--reason", "r"], tty=True, typed=_line_asked_for, capsys=capsys)
    assert rc == 2 and eng.has("ALERT")


# ------------------------------------------------------------------------------------------------------------------------------------------------------------------ a real subprocess, as the red team ran it
def test_a_real_process_with_stdin_closed_cannot_clear_the_stop(tmp_path):
    import subprocess
    root = str(tmp_path)
    _halted_book(root)
    r = subprocess.run([sys.executable, os.path.join(BOT, "tools", "ms_testnet_autopilot.py"), "capital", "--root", root, "--ack-halt"], stdin=subprocess.DEVNULL, capture_output=True, text=True, cwd=BOT,
                       env={"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path)})
    assert r.returncode == 2 and CT.anchored(root).load()["halted"] is True, (r.returncode, r.stdout[-300:], r.stderr[-300:])


# ------------------------------------------------------------------------------------------------------------------------------------------------------------------ review round (the independent review of 3502ce9)
def test_several_owner_actions_in_one_command_are_refused_and_nothing_is_applied(tmp_path, capsys):
    """Review: with two flags the first was applied and the second refused, while the message said 'nothing was changed'. One owner action per command, so nothing is ever half-applied."""
    root = str(tmp_path)
    _halted_book(root)
    before = CT.anchored(root).load()
    for tty in (False, True):
        rc, out = _run(["capital", "--root", root, "--ack-halt", "--reserve-fraction", "0.5"], tty=tty, typed=_line_asked_for, capsys=capsys)
        assert rc == 2 and "one owner action per command" in out and CT.anchored(root).load() == before, out
    assert [r["action"] for r in _log_rows(root)] == ["REFUSED_MULTIPLE", "REFUSED_MULTIPLE"]
    assert _log_rows(root)[0]["payload"]["actions"] == ["ACK-HALT", "SET-RESERVE"]


def test_end_of_input_at_the_prompt_is_a_refusal_with_a_trace_not_a_traceback(tmp_path, capsys):
    root = str(tmp_path)
    _halted_book(root)

    def eof(_prompt):
        raise EOFError
    rc = AP.main(["capital", "--root", root, "--ack-halt"], env={}, isatty=lambda: True, input_fn=eof, uid=1)
    out = capsys.readouterr().out
    assert rc == 2 and "no line was typed" in out and "nothing was changed" in out and CT.anchored(root).load()["halted"] is True
    assert [r["action"] for r in _log_rows(root)] == ["REFUSED_NO_INPUT"]


def test_the_intent_row_records_how_the_answer_arrived_not_only_that_it_did(tmp_path, capsys, monkeypatch):
    """The gate cannot tell a person from a script that reads the printed line back through a pseudo-terminal. It records what can be told apart afterwards: the time between the prompt and the answer, the
    parent process, and whether the session came in over SSH."""
    root = str(tmp_path)
    _halted_book(root)
    ticks = iter(range(10 ** 6))
    monkeypatch.setattr(AP.time, "time", lambda: 1_794_312_000.0 + next(ticks))                         # every clock read advances one second: the prompt-to-answer gap is whole seconds
    rc, out = _run(["capital", "--root", root, "--ack-halt"], tty=True, typed=_line_asked_for, capsys=capsys, env={"SSH_CONNECTION": "10.0.0.1 5000 10.0.0.2 22"})
    assert rc == 0, out
    intent = [r for r in _log_rows(root) if r["action"] == "INTENT"][0]["payload"]
    assert intent["ssh"] is True and intent["ppid"] == os.getppid() and (intent["parent_cmd"] is None or isinstance(intent["parent_cmd"], str))
    assert isinstance(intent["elapsed_ms"], int) and intent["elapsed_ms"] >= 1000
    _halted_book(str(tmp_path / "other"), account=CT.ACCOUNT2)
    rc, out = _run(["capital", "--root", str(tmp_path / "other"), "--ack-halt"], tty=True, typed=_line_asked_for, capsys=capsys, env={})
    assert [r for r in _log_rows(str(tmp_path / "other")) if r["action"] == "INTENT"][0]["payload"]["ssh"] is False


def test_a_refusal_for_the_missing_terminal_also_records_the_parent_and_ssh(tmp_path, capsys):
    root = str(tmp_path)
    _halted_book(root)
    rc, out = _run(["capital", "--root", root, "--ack-halt"], tty=False, capsys=capsys, env={"SSH_TTY": "/dev/pts/3"})
    row = _log_rows(root)[0]["payload"]
    assert rc == 2 and row["ssh"] is True and row["ppid"] == os.getppid() and "elapsed_ms" not in row


def test_a_closing_audit_row_that_cannot_be_written_is_loud_and_the_exit_code_says_so(tmp_path, capsys, monkeypatch):
    """The INTENT row is written before the action; if only the CLOSING row fails the action HAS happened, the message says so and the exit code is not 0."""
    root = str(tmp_path)
    _halted_book(root)
    real = ES.EngineState.audit

    def audit(self, name, action, payload):
        if action in ("APPLIED", "NOOP"):
            raise OSError(28, "No space left on device")
        return real(self, name, action, payload)
    monkeypatch.setattr(ES.EngineState, "audit", audit)
    rc, out = _run(["capital", "--root", root, "--ack-halt"], tty=True, typed=_line_asked_for, capsys=capsys)
    assert rc == 1 and "was applied but its closing audit row could not be written" in out and "INTENT row" in out, out
    assert CT.anchored(root).load()["halted"] is False
    assert [r["action"] for r in _log_rows(root)] == ["INTENT"]


def test_clear_alert_the_real_command_refusing_because_the_venue_is_not_flat_is_audited_as_not_applied(tmp_path, capsys, monkeypatch):
    root = str(tmp_path)
    eng = ES.EngineState(root, CLOCK)
    eng.set("ALERT", {"code": "UNRECONCILED"})
    real = AP.cmd_clear_alert
    monkeypatch.setattr(AP, "cmd_clear_alert", lambda e, r, clock, reason, **kw: real(e, r, clock, reason, recover_fn=lambda *a, **k: (False, ["NOT_FLAT:linear:BTCUSDT"], {}), out=print))
    rc, out = _run(["clear-alert", "--root", root, "--reason", "looked"], tty=True, typed=_line_asked_for, capsys=capsys)
    assert rc == 2 and eng.has("ALERT") and "the venue is not proven flat" in out, out
    rows = [r for r in _log_rows(root) if r["marker"] == "ALERT" and r["action"] != "SET"]                  # (the SET row is the alert itself)
    assert [r["action"] for r in rows] == ["INTENT", "NOT_APPLIED"] and rows[-1]["payload"]["after"]["alert_open"] is True and rows[-1]["payload"]["after"]["rc"] == 2
