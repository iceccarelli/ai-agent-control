"""tools/trial_journal.py: the one operator route for evaluating the frozen prospective plan. Synthetic data and temporary directories only.

The restart, race, crash and fault tests run REAL separate Python processes (``subprocess``); nothing here reloads a journal inside one process and
calls that a restart.
"""
from __future__ import annotations

import ast
import json
import os
import stat
import subprocess
import sys
import textwrap
import time

import pytest

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BOT, "tools"))
sys.path.insert(0, BOT)

import carry_prospective as CP  # noqa: E402
import statistical_power as SP  # noqa: E402
import trial_journal as TJ  # noqa: E402
from test_carry_prospective import CLOSED, window_prints  # noqa: E402

TESTS = os.path.dirname(os.path.abspath(__file__))


@pytest.fixture(autouse=True)
def _clock(monkeypatch):
    monkeypatch.setattr(CP, "_now_ms", lambda: CLOSED)


def look(path, seed=1, **kw):
    kw.setdefault("allow_unanchored", True)
    return TJ.evaluate_logged(window_prints(0.3, seed=seed), 10.0, str(path), **kw)


def anchor_of(rep):
    a = rep["journal"]["record_this_anchor_outside_the_journal"]
    return SP.LedgerAnchor(a["n"], a["head"])


# ---- a real child process: the clock is moved (not the argument) exactly as the unit tests do; ``mode`` injects one fault ---------------------
DRIVER = textwrap.dedent('''
    import errno, json, os, sys, time
    sys.path[:0] = [{tests!r}, {bot!r}, os.path.join({bot!r}, "tools")]
    import carry_prospective as CP, statistical_power as SP, trial_journal as TJ
    from test_carry_prospective import CLOSED, window_prints
    CP._now_ms = lambda: CLOSED
    cfg = json.loads(sys.argv[1])
    if cfg["mode"] == "crash_mid_append":
        real = os.write
        def half(fd, data):
            real(fd, bytes(data[: len(data) // 2]))
            os._exit(9)                           # killed with half a line on disk
        TJ._os_write = half
    elif cfg["mode"] == "short_writes":
        real = os.write
        TJ._os_write = lambda fd, data: real(fd, bytes(data[:7]))      # the kernel accepts 7 bytes at a time
    elif cfg["mode"] == "enospc":
        def full(fd, data):
            raise OSError(errno.ENOSPC, "No space left on device")
        TJ._os_write = full
    elif cfg["mode"] == "fsync_fails":
        def bad(fd):
            raise OSError(errno.EIO, "Input/output error")
        TJ._os_fsync = bad
    elif cfg["mode"] == "slow":
        real_eval = CP.evaluate
        def slow(*a, **k):
            time.sleep(cfg["sleep"])
            return real_eval(*a, **k)
        CP.evaluate = slow
    anchor = SP.LedgerAnchor(*cfg["anchor"]) if cfg.get("anchor") else None
    try:
        rep = TJ.evaluate_logged(window_prints(0.3, seed=cfg["seed"]), 10.0, cfg["journal"], anchor=anchor, first_look=cfg.get("first_look", False),
                                 allow_unanchored=cfg.get("allow_unanchored", False), lock_timeout_s=cfg.get("lock_timeout", 30.0))
    except Exception as exc:
        print(json.dumps({{"refused": type(exc).__name__, "why": str(exc)}}))
        sys.exit(3)
    print(json.dumps({{"journal": rep["journal"]}}))
''').format(tests=TESTS, bot=BOT)


def child(journal, seed=1, mode="ok", wait=True, **cfg):
    cfg.update(journal=str(journal), seed=seed, mode=mode)
    p = subprocess.Popen([sys.executable, "-c", DRIVER, json.dumps(cfg)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if not wait:
        return p
    out, err = p.communicate(timeout=120)
    return p.returncode, out, err


def parsed(out):
    return json.loads(out.strip().splitlines()[-1])


# ============================================================================================ restart: real processes

def test_a_separate_process_sees_the_previous_trials_and_a_fresh_process_cannot_restart_the_count(tmp_path):
    j = tmp_path / "looks.jsonl"
    seen = []
    for seed in (1, 2, 3):
        rc, out, err = child(j, seed, first_look=(seed == 1), allow_unanchored=True)
        assert rc == 0, err
        seen.append(parsed(out)["journal"]["n_trials"])
    assert seen == [1, 2, 3]                                               # three processes, one growing count
    assert TJ.inspect(str(j))["n_trials"] == 3


def test_a_fresh_process_with_no_first_look_and_no_anchor_refuses_instead_of_starting_an_empty_ledger(tmp_path):
    rc, out, _ = child(tmp_path / "looks.jsonl", 1)
    assert rc == 3 and parsed(out)["refused"] == "JournalError" and "first_look" in parsed(out)["why"]
    assert not (tmp_path / "looks.jsonl").exists()


def test_a_journal_that_exists_needs_an_anchor_or_an_explicit_acceptance_of_the_limit(tmp_path):
    j = tmp_path / "looks.jsonl"
    look(j, 1, first_look=True)
    rc, out, _ = child(j, 2)
    assert rc == 3 and "no anchor" in parsed(out)["why"]
    rep = look(j, 3)                                                       # explicitly unanchored: allowed, and it says so
    assert rep["journal"]["checked_against_an_anchor"] is False and "NO ANCHOR WAS CHECKED" in rep["journal"]["limit"]


def test_the_external_anchor_walks_forward_with_each_look_and_blocks_what_it_should(tmp_path):
    j = tmp_path / "looks.jsonl"
    a1 = anchor_of(look(j, 1, first_look=True))
    rc, out, err = child(j, 2, anchor=list(a1))
    assert rc == 0, err
    a2 = SP.LedgerAnchor(parsed(out)["journal"]["n_trials"], parsed(out)["journal"]["head"])
    assert a2.n == 2
    assert TJ.inspect(str(j), a2)["status"] == TJ.VERIFIED_AT_ANCHOR
    assert TJ.inspect(str(j), a1)["status"] == TJ.VERIFIED_AHEAD_OF_ANCHOR        # an older anchor is still extended by the journal
    rc, out, _ = child(j, 3, anchor=[a2.n + 5, a2.head])                   # an anchor from the future
    assert rc == 3 and parsed(out)["refused"] == "JournalError" and "SHORTENED" in parsed(out)["why"]


# ============================================================================================ the seven distinguishable states

def test_every_state_is_told_apart(tmp_path):
    j = tmp_path / "looks.jsonl"
    assert TJ.inspect(str(j))["status"] == TJ.NEVER_INITIALISED
    a1 = anchor_of(look(j, 1, first_look=True))
    a2 = anchor_of(look(j, 2))
    assert TJ.inspect(str(j))["status"] == TJ.UNANCHORED
    assert TJ.inspect(str(j), a2)["status"] == TJ.VERIFIED_AT_ANCHOR
    assert TJ.inspect(str(j), a1)["status"] == TJ.VERIFIED_AHEAD_OF_ANCHOR
    lines = j.read_text().splitlines(keepends=True)
    j.write_text(lines[0])                                                 # the tail removed
    assert TJ.inspect(str(j), a2)["status"] == TJ.SHORTENED
    rows = [json.loads(x) for x in lines]
    rows[0]["statistic"] += 1.0                                            # a rewrite of an early look
    j.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
    assert TJ.inspect(str(j), a2)["status"] == TJ.CORRUPT                  # internally inconsistent
    j.write_text("")
    j.unlink()
    assert TJ.inspect(str(j), a2)["status"] == TJ.REMOVED


def test_a_consistent_rewrite_is_caught_only_by_the_anchor(tmp_path):
    j = tmp_path / "looks.jsonl"
    look(j, 1, first_look=True)
    anchor = anchor_of(look(j, 2))
    j.unlink()
    for seed in (7, 8):                                                    # someone replays a different, perfectly valid history of the same length
        look(j, seed, first_look=(seed == 7))
    assert TJ.inspect(str(j))["status"] == TJ.UNANCHORED                   # internally flawless
    assert TJ.inspect(str(j), anchor)["status"] == TJ.REWRITTEN
    with pytest.raises(TJ.JournalError, match="REWRITTEN"):
        look(j, 9, anchor=anchor)


def test_a_removed_journal_is_refused_against_its_anchor_and_without_it_looks_like_a_first_look(tmp_path):
    j = tmp_path / "looks.jsonl"
    anchor = anchor_of(look(j, 1, first_look=True))
    j.unlink()
    with pytest.raises(TJ.JournalError, match="REMOVED"):
        look(j, 2, anchor=anchor, first_look=True)                         # even an explicit first_look cannot override a recorded anchor
    assert TJ.inspect(str(j))["status"] == TJ.NEVER_INITIALISED            # the limit of an unanchored journal, stated rather than hidden


def test_an_anchor_of_zero_or_garbage_is_not_an_anchor():
    for n, head in ((0, "a" * 64), (-1, "a" * 64), (True, "a" * 64), (1, "xyz"), (1, "A" * 64), (1, None), ("1", "a" * 64)):
        with pytest.raises(TJ.JournalError):
            TJ.parse_anchor(n, head)


def test_an_anchor_file_next_to_the_journal_is_refused_but_one_elsewhere_is_read(tmp_path):
    (tmp_path / "w").mkdir()
    (tmp_path / "away").mkdir()
    j = tmp_path / "w" / "looks.jsonl"
    a = anchor_of(look(j, 1, first_look=True))
    near, far = tmp_path / "w" / "anchor.txt", tmp_path / "away" / "anchor.txt"
    for f in (near, far):
        f.write_text(f"{a.n} {a.head}\n")
        os.chmod(f, 0o600)
    with pytest.raises(TJ.JournalError, match="journal's directory"):
        TJ.read_anchor_file(str(near), str(j))
    assert TJ.read_anchor_file(str(far), str(j)) == a
    far.write_text("1\n")
    with pytest.raises(TJ.JournalError):
        TJ.read_anchor_file(str(far), str(j))


# ============================================================================================ concurrency

def test_simultaneous_writers_get_distinct_trial_indices_and_the_chain_stays_valid(tmp_path):
    j = tmp_path / "looks.jsonl"
    procs = [child(j, s, wait=False, first_look=True, allow_unanchored=True, mode="slow", sleep=0.3) for s in range(1, 6)]
    results = [(p.wait(timeout=120), p.stdout.read(), p.stderr.read()) for p in procs]
    assert all(rc == 0 for rc, _, _ in results), [r[2] for r in results]
    counts = sorted(parsed(out)["journal"]["n_trials"] for _, out, _ in results)
    assert counts == [1, 2, 3, 4, 5]                                       # no two successful evaluations claimed the same index
    rep = TJ.inspect(str(j))
    assert rep["n_trials"] == 5 and rep["status"] == TJ.UNANCHORED
    assert [json.loads(x)["index"] for x in j.read_text().splitlines()] == [0, 1, 2, 3, 4]


def test_a_held_lock_blocks_a_second_evaluator_rather_than_letting_it_race(tmp_path):
    j = tmp_path / "looks.jsonl"
    look(j, 1, first_look=True)
    with TJ._Lock(str(j), 1.0):
        rc, out, _ = child(j, 2, lock_timeout=0.3)
    assert rc == 3 and parsed(out)["refused"] == "JournalLockError"
    assert TJ.inspect(str(j))["n_trials"] == 1


def test_a_killed_lock_holder_does_not_strand_the_lock(tmp_path):
    j = tmp_path / "looks.jsonl"
    look(j, 1, first_look=True)
    p = child(j, 2, wait=False, allow_unanchored=True, mode="slow", sleep=30)
    time.sleep(1.5)                                                        # it holds the lock and is sleeping inside the evaluation
    p.kill()
    p.wait()
    rc, out, err = child(j, 3, allow_unanchored=True, lock_timeout=5)
    assert rc == 0, err
    assert parsed(out)["journal"]["n_trials"] == 2                         # the killed look never happened, and nothing is half-written


def test_a_writer_that_ignores_the_lock_is_detected_by_compare_and_append(tmp_path, monkeypatch):
    j = tmp_path / "looks.jsonl"
    look(j, 1, first_look=True)
    real = CP.evaluate

    def sneaky(*a, **k):
        fd = os.open(j, os.O_WRONLY | os.O_APPEND)                           # a rogue writer appends while we hold the lock
        os.write(fd, b"\n")
        os.close(fd)
        return real(*a, **k)
    monkeypatch.setattr(CP, "evaluate", sneaky)
    with pytest.raises(TJ.JournalError, match="ignored the lock"):
        look(j, 2)


# ============================================================================================ crash and write faults

def test_a_writer_killed_mid_append_makes_no_success_claim_and_blocks_the_next_evaluation(tmp_path):
    j = tmp_path / "looks.jsonl"
    look(j, 1, first_look=True)
    good = j.read_bytes()
    rc, out, _ = child(j, 2, mode="crash_mid_append", allow_unanchored=True)
    assert rc == 9 and out.strip() == ""                                   # it died; it printed no report
    torn = j.read_bytes()
    assert torn.startswith(good) and len(torn) > len(good) and not torn.endswith(b"\n")
    for _ in range(2):                                                     # not repaired, not truncated, and it keeps refusing
        rc, out, _ = child(j, 3, allow_unanchored=True)
        assert rc == 3 and "partial line" in parsed(out)["why"]
        assert j.read_bytes() == torn
    assert TJ.inspect(str(j))["status"] == TJ.CORRUPT


def test_short_writes_are_completed(tmp_path):
    j = tmp_path / "looks.jsonl"
    rc, out, err = child(j, 1, mode="short_writes", first_look=True, allow_unanchored=True)
    assert rc == 0, err
    assert TJ.inspect(str(j))["n_trials"] == 1 and j.read_bytes().endswith(b"\n")


@pytest.mark.parametrize("mode", ["enospc", "fsync_fails"])
def test_a_failed_write_or_fsync_is_an_error_never_a_reported_look(tmp_path, mode):
    j = tmp_path / "looks.jsonl"
    look(j, 1, first_look=True)
    before = j.read_bytes()
    rc, out, _ = child(j, 2, mode=mode, allow_unanchored=True)
    assert rc == 3 and parsed(out)["refused"] == "JournalWriteError" and "NOT made" in parsed(out)["why"]
    assert "journal" not in parsed(out)
    if mode == "enospc":
        assert j.read_bytes() == before
    else:
        # the line reached the file but its durability is unknown: it is counted (conservative for multiple testing), never hidden, and never reported as a result
        assert TJ.inspect(str(j))["n_trials"] in (1, 2)


def test_a_failed_first_append_leaves_no_phantom_look(tmp_path):
    j = tmp_path / "looks.jsonl"
    rc, out, _ = child(j, 1, mode="enospc", first_look=True, allow_unanchored=True)
    assert rc == 3 and parsed(out)["refused"] == "JournalWriteError"
    assert TJ.inspect(str(j))["n_trials"] == 0


def test_a_refused_evaluation_appends_nothing(tmp_path, monkeypatch):
    j = tmp_path / "looks.jsonl"
    look(j, 1, first_look=True)
    before = j.read_text()
    monkeypatch.setattr(CP, "_now_ms", lambda: CP.WINDOW_START_MS)
    with pytest.raises(SP.WindowCoverageError):
        look(j, 2)
    assert j.read_text() == before


# ============================================================================================ tampering that needs no anchor

@pytest.mark.parametrize("edit", [
    lambda t: t.replace('"statistic": ', '"statistic": 9', 1),
    lambda t: t.replace('"index": 1', '"index": 0', 1),
    lambda t: t.rstrip("\n"),
    lambda t: t + "not json\n",
    lambda t: t + "\n",
    lambda t: t.splitlines(keepends=True)[1] + t.splitlines(keepends=True)[0],
    lambda t: t.replace("{", '{"extra": 1,', 1),
    lambda t: t.replace('"pvalue": ', '"pvalue": NaN, "x": ', 1),
])
def test_an_edited_reordered_or_partial_journal_is_refused_by_a_fresh_process(tmp_path, edit):
    j = tmp_path / "looks.jsonl"
    look(j, 1, first_look=True)
    look(j, 2)
    j.write_text(edit(j.read_text()))
    rc, out, _ = child(j, 3, allow_unanchored=True)
    assert rc == 3 and parsed(out)["refused"] == "JournalError" and "CORRUPT" in parsed(out)["why"]


def test_an_edit_to_the_last_look_alone_is_caught_by_its_stored_hash(tmp_path):
    j = tmp_path / "looks.jsonl"
    look(j, 1, first_look=True)
    row = json.loads(j.read_text())
    row["statistic"] += 1.0                                                # nothing follows it, so only its own hash can expose this
    j.write_text(json.dumps(row, sort_keys=True) + "\n")
    info = TJ.inspect(str(j))
    assert info["status"] == TJ.CORRUPT and "does not match" in info["why"]


def test_the_journal_is_utf8_and_size_bounded(tmp_path):
    j = tmp_path / "looks.jsonl"
    look(j, 1, first_look=True)
    j.write_bytes(b"\xff\xfe\n")
    assert TJ.inspect(str(j))["status"] == TJ.CORRUPT
    j.write_bytes(b"x" * (TJ.MAX_JOURNAL_BYTES + 1))
    os.chmod(j, 0o600)
    with pytest.raises(TJ.JournalError, match="larger than"):
        TJ.inspect(str(j))


# ============================================================================================ path and file integrity

def test_symlinks_dangling_or_not_are_refused_and_nothing_is_created_through_them(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("precious")
    j = tmp_path / "looks.jsonl"
    os.symlink(tmp_path / "does_not_exist", j)                            # DANGLING: an exists() test is False for it
    with pytest.raises(TJ.JournalError, match="symbolic link"):
        look(j, 1, first_look=True)
    assert not (tmp_path / "does_not_exist").exists()
    j.unlink()
    os.symlink(victim, j)
    with pytest.raises(TJ.JournalError, match="symbolic link"):
        look(j, 1, first_look=True)
    with pytest.raises(TJ.JournalError, match="symbolic link"):
        TJ.inspect(str(j))
    assert victim.read_text() == "precious"


def test_a_symlinked_lock_file_is_refused(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("precious")
    j = tmp_path / "looks.jsonl"
    os.symlink(victim, str(j) + ".lock")
    with pytest.raises(TJ.JournalError, match="symbolic link"):
        look(j, 1, first_look=True)
    assert victim.read_text() == "precious"


def test_a_directory_symlink_for_the_journal_directory_is_refused(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    os.symlink(real, link)
    with pytest.raises(TJ.JournalError, match="real directory"):
        look(link / "looks.jsonl", 1, first_look=True)


def test_a_directory_or_a_hard_linked_file_is_not_a_journal(tmp_path):
    d = tmp_path / "looks.jsonl"
    d.mkdir()
    with pytest.raises((TJ.JournalError, _AR.RecordError)):                 # the shared guard (a directory has link count 2) or the opened descriptor's own check
        TJ.inspect(str(d))
    j = tmp_path / "other.jsonl"
    look(j, 1, first_look=True)
    twin = tmp_path / "twin.jsonl"
    os.link(j, twin)
    with pytest.raises((TJ.JournalError, _AR.RecordError), match="hard link"):       # the shared protected-path guard or the opened descriptor's own check
        TJ.inspect(str(j))


def test_files_are_created_private_and_a_group_writable_journal_is_refused(tmp_path):
    j = tmp_path / "looks.jsonl"
    look(j, 1, first_look=True)
    assert stat.S_IMODE(os.stat(j).st_mode) == 0o600 and stat.S_IMODE(os.stat(str(j) + ".lock").st_mode) == 0o600
    os.chmod(j, 0o664)
    with pytest.raises(TJ.JournalError, match="writable by group or others"):
        TJ.inspect(str(j))


@pytest.mark.parametrize("name", ["trading_state.db", "state.db-wal", ".env", "runs_evidence.jsonl", "api_key.jsonl", "secrets.jsonl"])
def test_protected_names_are_never_a_journal(tmp_path, name):
    with pytest.raises(AR_ERR):
        look(tmp_path / name, 1, first_look=True)
    assert not (tmp_path / name).exists() and not (tmp_path / (name + ".lock")).exists()


import account_review as _AR  # noqa: E402
AR_ERR = _AR.RecordError


# ============================================================================================ the operator route and the pure calculation

def test_the_journal_route_is_the_same_calculation_as_the_pure_function(tmp_path):
    prints = window_prints(2.5, seed=3)
    pure = CP.evaluate(prints, 10.0, SP.TrialLedger())
    logged = TJ.evaluate_logged(prints, 10.0, str(tmp_path / "looks.jsonl"), first_look=True)
    logged.pop("journal")
    assert json.dumps(logged, sort_keys=True, default=str) == json.dumps(pure, sort_keys=True, default=str)


def test_a_second_look_is_corrected_for_the_first_across_a_restart(tmp_path):
    j = tmp_path / "looks.jsonl"
    first = look(j, 1, first_look=True)
    rc, out, err = child(j, 2, allow_unanchored=True)
    assert rc == 0, err
    pure_second = CP.evaluate(window_prints(0.3, seed=2), 10.0, SP.TrialLedger())
    after_restart = TJ.evaluate_logged(window_prints(0.3, seed=2), 10.0, str(j), allow_unanchored=True)
    assert after_restart["journal"]["n_trials"] == 3 and first["journal"]["n_trials"] == 1
    assert json.dumps(after_restart, default=str) != json.dumps(pure_second, default=str)       # carrying the earlier looks changes the corrected result's trial count


def test_carry_prospective_cli_can_report_progress_but_has_no_way_to_evaluate():
    src = open(CP.__file__, encoding="utf-8").read()
    tree = ast.parse(src)
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    flags = {a.value for n in ast.walk(main) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "add_argument" for a in n.args if isinstance(a, ast.Constant)}
    assert "evaluate" not in " ".join(f for f in flags if isinstance(f, str))
    calls = {getattr(n.func, "attr", getattr(n.func, "id", "")) for n in ast.walk(main) if isinstance(n, ast.Call)}
    assert "evaluate" not in calls                                          # the CLI cannot reach the unprotected evaluation
    assert "trial_journal" in (CP.__doc__ or "")                             # and its docstring sends operators to the supported route


def test_the_cli_verifies_evaluates_and_prints_the_anchor_to_keep(tmp_path, capsys):
    (tmp_path / "w").mkdir()
    (tmp_path / "away").mkdir()
    f = tmp_path / "w" / "funding.jsonl"
    f.write_text("".join(json.dumps({"ts_ms": t, "funding_bps": v}) + "\n" for t, v in window_prints(0.3, seed=1)))
    j = tmp_path / "w" / "looks.jsonl"
    assert TJ.main(["verify", "--journal", str(j)]) == 3                  # nothing initialised: not OK to evaluate without saying so
    assert TJ.main(["evaluate", "--funding", str(f), "--journal", str(j)]) == 2          # refuses: no first-look declaration
    assert TJ.main(["evaluate", "--funding", str(f), "--journal", str(j), "--first-look", "--allow-unanchored"]) == 0
    err = capsys.readouterr().err
    n, head = err.strip().splitlines()[-1].split()[-2:]
    assert n == "1" and len(head) == 64
    anchor = tmp_path / "away" / "anchor.txt"
    anchor.write_text(f"{n} {head}\n")
    os.chmod(anchor, 0o600)
    assert TJ.main(["verify", "--journal", str(j), "--anchor-file", str(anchor)]) == 0
    assert TJ.main(["evaluate", "--funding", str(f), "--journal", str(j), "--anchor-file", str(anchor)]) == 0
    assert TJ.main(["verify", "--journal", str(j), "--anchor-n", "1", "--anchor-head", "0" * 64]) == 3      # wrong head: rewritten


def test_the_module_touches_no_network_process_or_database():
    tree = ast.parse(open(TJ.__file__, encoding="utf-8").read())
    imported = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            imported |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            imported.add(n.module.split(".")[0])
    assert imported <= {"__future__", "argparse", "errno", "fcntl", "json", "os", "re", "stat", "sys", "time", "typing", "account_review", "carry_prospective",
                        "statistical_power"}, imported
    calls = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert not calls & {"exec", "eval", "compile", "__import__"}
