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
    kw.setdefault("accept_data_revision", True)          # most tests use different seeds (different prints) per look; the data rule has its own tests
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
    elif cfg["mode"] == "dir_fsync_fails":
        calls = []
        real_fsync = os.fsync
        def second(fd):
            calls.append(fd)
            if len(calls) == 2:
                raise OSError(errno.EIO, "Input/output error")
            real_fsync(fd)
        TJ._os_fsync = second
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
                                 allow_unanchored=cfg.get("allow_unanchored", False), accept_data_revision=cfg.get("accept_data_revision", True),
                                 lock_timeout_s=cfg.get("lock_timeout", 30.0))
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
    assert rc == 3 and parsed(out)["refused"] == "JournalWriteError" and "MAY OR MAY NOT" in parsed(out)["why"]
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
    logged.pop("selection_sensitivity")
    assert json.dumps(logged, sort_keys=True, default=str) == json.dumps(pure, sort_keys=True, default=str)


def test_a_second_look_is_corrected_for_the_first_across_a_restart(tmp_path):
    j = tmp_path / "looks.jsonl"
    first = look(j, 1, first_look=True)
    rc, out, err = child(j, 2, allow_unanchored=True)
    assert rc == 0, err
    pure_second = CP.evaluate(window_prints(0.3, seed=2), 10.0, SP.TrialLedger())
    after_restart = TJ.evaluate_logged(window_prints(0.3, seed=2), 10.0, str(j), allow_unanchored=True, accept_data_revision=True)
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
    line = next(x for x in err.splitlines() if x.startswith("ANCHOR TO RECORD NOW"))
    assert "HIGHEST count" in err and "WRITER CANNOT INFLUENCE" in line
    n, head = line.split()[-2:]
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
    assert imported <= {"__future__", "argparse", "errno", "fcntl", "hashlib", "json", "math", "os", "re", "stat", "sys", "time", "typing", "account_review", "carry_prospective",
                        "statistical_power"}, imported
    calls = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert not calls & {"exec", "eval", "compile", "__import__"}


# ============================================================================================ independent security review (reproduced hangs and races)

def _run_cli(args, timeout=10):
    return subprocess.run([sys.executable, os.path.join(BOT, "tools", "trial_journal.py"), *args], capture_output=True, text=True, timeout=timeout)


def test_a_fifo_as_journal_or_anchor_is_refused_not_a_hang(tmp_path):
    (tmp_path / "w").mkdir()
    (tmp_path / "away").mkdir()
    fifo_j, fifo_a = tmp_path / "w" / "fifo.jsonl", tmp_path / "away" / "anchor"
    os.mkfifo(fifo_j)
    os.mkfifo(fifo_a)
    r = _run_cli(["verify", "--journal", str(fifo_j)])                                  # timeout=10 turns a hang into a test failure
    assert r.returncode == 2 and "not a regular file" in r.stderr
    j = tmp_path / "w" / "x.jsonl"
    r = _run_cli(["verify", "--journal", str(j), "--anchor-file", str(fifo_a)])
    assert r.returncode == 2 and "not a regular file" in r.stderr


def test_an_oversized_or_non_utf8_anchor_file_is_refused(tmp_path):
    (tmp_path / "w").mkdir()
    (tmp_path / "away").mkdir()
    j = tmp_path / "w" / "looks.jsonl"
    f = tmp_path / "away" / "anchor.txt"
    f.write_text("1 " + "0" * 64 + " " * 5000 + "junk\n")
    os.chmod(f, 0o600)
    with pytest.raises(TJ.JournalError, match="larger than any anchor"):
        TJ.read_anchor_file(str(f), str(j))
    f.write_bytes(b"\xff\xfe")
    with pytest.raises(TJ.JournalError, match="UTF-8"):
        TJ.read_anchor_file(str(f), str(j))


def test_the_open_is_relative_to_a_verified_directory_so_a_swapped_ancestor_cannot_redirect_it(tmp_path, monkeypatch):
    # Swap the journal's directory for a link to a protected-looking tree in the window between the checks and the open.
    good = tmp_path / "good"
    good.mkdir()
    evil = tmp_path / "bot" / "state"
    evil.mkdir(parents=True)
    real_open = os.open
    swapped = {"done": False}

    def swapping_open(path, flags, *a, **k):
        if not swapped["done"] and str(path) == str(good) and flags & os.O_DIRECTORY:
            swapped["done"] = True
            good.rmdir()
            os.symlink(evil, good)                                         # the directory the caller named becomes a link just before it is opened
        return real_open(path, flags, *a, **k)
    monkeypatch.setattr(os, "open", swapping_open)
    with pytest.raises((TJ.JournalError, _AR.RecordError, OSError)):
        look(good / "looks.jsonl", 1, first_look=True)
    assert list(evil.iterdir()) == []                                      # nothing was created under the protected tree


# ============================================================================================ data binding (the reproduced selection attack)

REQUIRED_10 = 1.1312328767              # the funding the 10 % hurdle requires per settlement (closed form, written out in test_carry_prospective)


def _null_true_prints_with_the_lowest_dropped(seed=9, k=86):
    full = window_prints(REQUIRED_10, seed=seed)                          # the true mean equals the requirement: the null is TRUE
    keep = set(sorted(full, key=lambda x: x[1])[k:])
    return full, [p for p in full if p in keep]


def test_dropping_the_lowest_prints_passes_the_frozen_floor_but_is_flagged_and_cannot_be_swapped_in_silently(tmp_path):
    full, cherry = _null_true_prints_with_the_lowest_dropped()
    j = tmp_path / "looks.jsonl"
    honest = TJ.evaluate_logged(full, 10.0, str(j), first_look=True)
    assert "SUPPORTED" not in honest["verdict"]                           # the full data: no edge, as the truth is
    with pytest.raises(TJ.JournalError, match="data digest differs"):    # the cherry-picked set cannot be swapped in as a later look
        TJ.evaluate_logged(cherry, 10.0, str(j), allow_unanchored=True)
    assert TJ.inspect(str(j))["n_trials"] == 1
    j2 = tmp_path / "cherry.jsonl"                                          # evaluated alone, the registered verdict is wrongly favourable ...
    rep = TJ.evaluate_logged(cherry, 10.0, str(j2), first_look=True)
    assert "SUPPORTED" in rep["verdict"] and CP.coverage(cherry, CP.WINDOW_END_MS)["coverage"] >= CP.MIN_COVERAGE
    s = rep["selection_sensitivity"]                                       # ... and the report says so instead of hiding it
    assert s["flipped_by_lowest_observed_fill"] is True and "SUPPORTED" not in str(s["lowest_observed_fill_verdict"]) and s["missing_settlements"] == 86
    assert "DO NOT RELY" in s["note"]


def test_a_complete_data_set_has_nothing_missing_to_fill(tmp_path):
    full, _ = _null_true_prints_with_the_lowest_dropped()
    rep = TJ.evaluate_logged(full, 10.0, str(tmp_path / "j.jsonl"), first_look=True)
    assert rep["selection_sensitivity"]["flipped_by_lowest_observed_fill"] is False and rep["selection_sensitivity"]["missing_settlements"] == 0


def test_the_data_digest_is_in_the_chain_so_it_cannot_be_edited_afterwards(tmp_path):
    j = tmp_path / "looks.jsonl"
    full, cherry = _null_true_prints_with_the_lowest_dropped()
    TJ.evaluate_logged(full, 10.0, str(j), first_look=True)
    row = json.loads(j.read_text())
    assert row["data"]["n_prints"] == len(full) and len(row["data"]["sha256"]) == 64 and row["data"]["revision"] is False
    row["data"]["sha256"] = TJ.data_binding(cherry, 10.0, False)["sha256"]
    j.write_text(json.dumps(row, sort_keys=True) + "\n")
    info = TJ.inspect(str(j))
    assert info["status"] == TJ.CORRUPT and "hash chain" in info["why"]


def test_the_same_data_may_be_looked_at_again_and_an_accepted_revision_is_recorded_in_the_chain(tmp_path):
    full, cherry = _null_true_prints_with_the_lowest_dropped()
    j = tmp_path / "looks.jsonl"
    TJ.evaluate_logged(full, 10.0, str(j), first_look=True)
    again = TJ.evaluate_logged(full, 10.0, str(j), allow_unanchored=True)
    assert again["journal"]["data"]["revision"] is False and again["journal"]["n_trials"] == 2
    rev = TJ.evaluate_logged(cherry, 10.0, str(j), allow_unanchored=True, accept_data_revision=True)
    assert rev["journal"]["data"]["revision"] is True
    assert [json.loads(x)["data"]["revision"] for x in j.read_text().splitlines()] == [False, False, True]


def test_the_cli_refuses_changed_data_unless_told_and_says_so(tmp_path, capsys):
    full, cherry = _null_true_prints_with_the_lowest_dropped()
    (tmp_path / "w").mkdir()
    f1, f2 = tmp_path / "w" / "full.jsonl", tmp_path / "w" / "cherry.jsonl"
    for f, pr in ((f1, full), (f2, cherry)):
        f.write_text("".join(json.dumps({"ts_ms": t, "funding_bps": v}) + "\n" for t, v in pr))
    j = str(tmp_path / "w" / "looks.jsonl")
    assert TJ.main(["evaluate", "--funding", str(f1), "--journal", j, "--first-look", "--allow-unanchored"]) == 0
    assert TJ.main(["evaluate", "--funding", str(f2), "--journal", j, "--allow-unanchored"]) == 2
    assert "data digest differs" in capsys.readouterr().err
    assert TJ.main(["evaluate", "--funding", str(f2), "--journal", j, "--allow-unanchored", "--accept-data-revision"]) == 0


def test_a_fractional_settlement_time_is_refused_not_snapped(tmp_path):
    f = tmp_path / "p.jsonl"
    f.write_text(json.dumps({"ts_ms": CP.WINDOW_START_MS + 0.9, "funding_bps": 1.0}) + "\n")
    with pytest.raises(_AR.RecordError, match="needs ts_ms"):
        CP._load(str(f))


# ============================================================================================ POSIX findings from the independent review

def test_an_append_that_would_land_on_a_renamed_inode_is_refused_not_reported(tmp_path, monkeypatch):
    j = tmp_path / "looks.jsonl"
    look(j, 1, first_look=True)
    real = CP.evaluate

    def rotate(*a, **k):
        os.rename(j, tmp_path / "aside.jsonl")                              # the journal is rotated away while the lock is held ...
        j.write_bytes(b"")                                                  # ... and an empty file takes its place
        os.chmod(j, 0o600)
        return real(*a, **k)
    monkeypatch.setattr(CP, "evaluate", rotate)
    with pytest.raises(TJ.JournalError, match="replaced or renamed|ignored the lock"):
        look(j, 2)
    assert (tmp_path / "looks.jsonl").read_bytes() == b""                   # nothing was claimed anywhere


def test_a_lock_taken_on_a_stale_inode_is_dropped_and_retaken_on_the_file_at_the_path(tmp_path, monkeypatch):
    j = tmp_path / "looks.jsonl"
    calls = []
    real = TJ._same_inode

    def once_stale(fd, path):
        calls.append(path)
        return False if len(calls) == 1 else real(fd, path)
    monkeypatch.setattr(TJ, "_same_inode", once_stale)
    look(j, 1, first_look=True)
    assert len([c for c in calls if c.endswith(".lock")]) >= 2 and TJ.inspect(str(j))["n_trials"] == 1


def test_a_reader_that_meets_a_partial_line_rechecks_under_the_lock_before_calling_it_corrupt(tmp_path, monkeypatch):
    j = tmp_path / "looks.jsonl"
    look(j, 1, first_look=True)
    whole = j.read_bytes()
    real, calls = TJ._read_journal, []

    def torn_then_whole(path):
        calls.append(1)
        return whole[:-5] if len(calls) == 1 else real(path)                # the first read raced a writer's append
    monkeypatch.setattr(TJ, "_read_journal", torn_then_whole)
    assert TJ.inspect(str(j))["status"] == TJ.UNANCHORED and len(calls) == 2
    monkeypatch.setattr(TJ, "_read_journal", lambda path: whole[:-5])         # a partial line that persists is still corrupt
    assert TJ.inspect(str(j))["status"] == TJ.CORRUPT


def test_a_failed_directory_fsync_on_first_create_is_a_write_error_not_a_raw_oserror(tmp_path):
    rc, out, _ = child(tmp_path / "looks.jsonl", 1, mode="dir_fsync_fails", first_look=True, allow_unanchored=True)
    assert rc == 3 and parsed(out)["refused"] == "JournalWriteError" and "MAY OR MAY NOT" in parsed(out)["why"]


def test_a_same_size_copy_swapped_in_under_the_lock_is_caught_before_anything_is_written(tmp_path, monkeypatch):
    # Same bytes, different inode: only the inode comparison (not the size check) can see it, and it must fire BEFORE the append.
    j = tmp_path / "looks.jsonl"
    look(j, 1, first_look=True)
    original = j.read_bytes()
    real = CP.evaluate

    def swap(*a, **k):
        os.rename(j, tmp_path / "aside.jsonl")
        j.write_bytes(original)
        os.chmod(j, 0o600)
        return real(*a, **k)
    monkeypatch.setattr(CP, "evaluate", swap)
    with pytest.raises(TJ.JournalError, match="replaced or renamed while it was held"):
        look(j, 2)
    assert j.read_bytes() == original and (tmp_path / "aside.jsonl").read_bytes() == original


# ============================================================================================ second independent review (fresh reviewer)

def test_a_failed_first_append_leaves_an_empty_file_that_does_not_demand_an_anchor_and_a_retry_works(tmp_path):
    j = tmp_path / "looks.jsonl"
    rc, out, _ = child(j, 1, mode="enospc", first_look=True, allow_unanchored=True)
    assert rc == 3 and j.exists() and j.stat().st_size == 0
    assert TJ.inspect(str(j))["status"] == TJ.NEVER_INITIALISED
    rep = TJ.evaluate_logged(window_prints(0.3, seed=1), 10.0, str(j), first_look=True)          # no allow_unanchored needed
    assert rep["journal"]["n_trials"] == 1
    with pytest.raises(TJ.JournalError, match="first_look"):                                    # an EMPTY file is not an excuse to skip first_look
        TJ.evaluate_logged(window_prints(0.3, seed=2), 10.0, str(tmp_path / "e.jsonl.x"))


def test_an_empty_journal_against_a_recorded_anchor_is_shortened_not_a_fresh_start(tmp_path):
    j = tmp_path / "looks.jsonl"
    anchor = anchor_of(look(j, 1, first_look=True))
    j.write_bytes(b"")
    os.chmod(j, 0o600)
    assert TJ.inspect(str(j), anchor)["status"] == TJ.SHORTENED
    with pytest.raises(TJ.JournalError, match="SHORTENED"):
        look(j, 2, anchor=anchor, first_look=True)


def test_a_journal_ahead_of_its_anchor_can_be_evaluated_and_the_new_anchor_walks_forward(tmp_path):
    j = tmp_path / "looks.jsonl"
    a1 = anchor_of(look(j, 1, first_look=True))
    look(j, 2)                                                           # a look whose anchor was never recorded
    rep = look(j, 3, anchor=a1, allow_unanchored=False)
    assert rep["journal"]["state_before"] == TJ.VERIFIED_AHEAD_OF_ANCHOR and rep["journal"]["n_trials"] == 3
    assert TJ.inspect(str(j), anchor_of(rep))["status"] == TJ.VERIFIED_AT_ANCHOR


def test_the_selection_check_changes_neither_the_journal_nor_the_ledger_and_cannot_lose_the_report(tmp_path, monkeypatch):
    full, cherry = _null_true_prints_with_the_lowest_dropped()
    j = tmp_path / "looks.jsonl"
    rep = TJ.evaluate_logged(cherry, 10.0, str(j), first_look=True)
    on_disk = j.read_bytes()
    assert len(on_disk.splitlines()) == 1 and rep["selection_sensitivity"]["flipped_by_lowest_observed_fill"] is True    # the shadow evaluation ran ...
    assert TJ.inspect(str(j))["n_trials"] == 1 and j.read_bytes() == on_disk                                  # ... and wrote nothing
    monkeypatch.setattr(TJ, "_selection_sensitivity", lambda *a, **k: (_ for _ in ()).throw(ZeroDivisionError("boom")))
    j2 = tmp_path / "second.jsonl"
    rep2 = TJ.evaluate_logged(cherry, 10.0, str(j2), first_look=True)
    assert rep2["journal"]["n_trials"] == 1 and rep2["selection_sensitivity"]["flipped_by_lowest_observed_fill"] is None
    assert "NOT checked for selection" in rep2["selection_sensitivity"]["note"]


def test_a_waiting_evaluator_really_relocks_the_file_at_the_path_when_its_lock_file_is_replaced(tmp_path):
    # Real processes, no fakes. A waits on the lock file's old inode; the file is unlinked and the holder leaves. A must end up holding the lock on the
    # NEW file, so a third evaluator D is shut out while A works. If A stayed on the stale inode, D would walk straight in.
    j = tmp_path / "looks.jsonl"
    look(j, 1, first_look=True)
    holder = TJ._Lock(str(j), 5.0)
    holder.__enter__()
    a = child(j, 2, wait=False, allow_unanchored=True, mode="slow", sleep=4)
    time.sleep(1.5)                                                      # A has opened the old lock file and is waiting on it
    os.unlink(str(j) + ".lock")
    holder.__exit__()                                                    # releasing the old inode: A wakes up on a file that is no longer at the path
    time.sleep(1.0)
    rc, out, _ = child(j, 3, allow_unanchored=True, lock_timeout=0.5)
    assert rc == 3 and parsed(out)["refused"] == "JournalLockError"
    assert a.wait(timeout=60) == 0
    assert TJ.inspect(str(j))["n_trials"] == 2


# ============================================================================================ third review: custody wording and what the sensitivity check is

def test_the_sensitivity_check_is_named_for_what_it_is_and_reports_the_break_even_fill(tmp_path):
    # Reproduced by the red team: SUPPORTED at the lowest observed fill, yet a legal lower fill flips it. The old key said 'worst_case'.
    full, cherry = _null_true_prints_with_the_lowest_dropped()
    rep = TJ.evaluate_logged(cherry, 10.0, str(tmp_path / "j.jsonl"), first_look=True, venue_floor_bps=-33.3)
    s = rep["selection_sensitivity"]
    assert "worst" not in json.dumps(s).lower().replace("not a worst-case bound", "").replace("not a worst case", "")
    assert "NOT a worst-case bound" in s["method"]
    assert s["lowest_observed_fill_bps"] == min(v for _, v in cherry)
    be = s["breakeven_fill_bps"]
    assert be is not None and be <= s["lowest_observed_fill_bps"] + 1e-9                 # the verdict needs the missing prints to be at least this low to flip
    assert s["venue_floor_fill_bps"] == -33.3 and s["venue_floor_source"] == "OWNER_SUPPLIED_UNVERIFIED"
    assert s["venue_floor_fill_verdict"] is not None


def test_a_verdict_that_survives_the_lowest_observed_fill_is_not_called_robust(tmp_path):
    # window_prints(2.0, seed=3) minus its 87 lowest: SUPPORTED at the lowest observed fill (reproduced), flips only at about -2 bps.
    full = window_prints(2.0, seed=3)
    keep = set(sorted(full, key=lambda x: x[1])[87:])
    rep = TJ.evaluate_logged([p for p in full if p in keep], 10.0, str(tmp_path / "j.jsonl"), first_look=True)
    s = rep["selection_sensitivity"]
    assert "SUPPORTED" in rep["verdict"] and s["flipped_by_lowest_observed_fill"] is False
    assert "NOT a worst case" in s["note"] and "does NOT show the verdict is robust" in s["note"]
    assert s["breakeven_fill_bps"] is not None and s["breakeven_fill_bps"] < s["lowest_observed_fill_bps"]       # a legal lower fill flips it


def test_the_module_does_not_claim_an_independent_trust_boundary_it_does_not_enforce():
    text = open(TJ.__file__, encoding="utf-8").read()
    for claim in ("tamper-evident", "kept where the writer cannot reach", "somewhere the writer cannot reach", "OUTSIDE THE JOURNAL'S HOST"):
        assert claim not in text, claim
    assert "NOT a trust boundary" in text and "STALE anchor verifies a truncated journal" in text


def test_a_stale_anchor_verifies_a_truncated_journal_which_is_why_the_operator_must_use_the_highest_count(tmp_path):
    j = tmp_path / "looks.jsonl"
    a1 = anchor_of(look(j, 1, first_look=True))
    look(j, 2)
    look(j, 3)
    lines = j.read_text().splitlines(keepends=True)
    j.write_text(lines[0])                                              # rolled back to the first look
    assert TJ.inspect(str(j), a1)["status"] == TJ.VERIFIED_AT_ANCHOR    # the code CANNOT know a1 is stale: the documented limit, pinned
    rep = look(j, 4, anchor=a1)
    assert rep["journal"]["n_trials"] == 2                              # two looks were lost to the rollback


def _independent_verdict(prints_present, fill):
    # Independent of the module under test: the registered test on an EMPTY throwaway ledger (a first look), missing settlements filled at ``fill``.
    have = dict(prints_present)
    grid = [CP.WINDOW_START_MS + i * CP.SETTLE_MS for i in range(CP.WINDOW_DAYS * CP.PER_DAY)]
    return CP.evaluate([(t, have.get(t, fill)) for t in grid], 10.0, SP.TrialLedger())["verdict"]


def test_the_break_even_fill_really_is_where_the_verdict_flips_and_the_owner_floor_is_really_used(tmp_path):
    full = window_prints(2.0, seed=3)                                    # SUPPORTED, and still SUPPORTED at the lowest observed fill (reproduced)
    keep = set(sorted(full, key=lambda x: x[1])[87:])
    present = [p for p in full if p in keep]
    s = TJ.evaluate_logged(present, 10.0, str(tmp_path / "j.jsonl"), first_look=True, venue_floor_bps=-33.3)["selection_sensitivity"]
    be = s["breakeven_fill_bps"]
    assert s["flipped_by_lowest_observed_fill"] is False and be < s["lowest_observed_fill_bps"]
    assert "SUPPORTED" in _independent_verdict(present, be + 0.01) and "SUPPORTED" not in _independent_verdict(present, be - 0.01)    # the flip point, both sides
    assert s["venue_floor_fill_verdict"] == _independent_verdict(present, -33.3) and "SUPPORTED" not in s["venue_floor_fill_verdict"]
    assert s["lowest_observed_fill_verdict"] == _independent_verdict(present, s["lowest_observed_fill_bps"])


def test_when_the_lowest_observed_fill_already_flips_the_break_even_says_so(tmp_path):
    full, cherry = _null_true_prints_with_the_lowest_dropped()
    s = TJ.evaluate_logged(cherry, 10.0, str(tmp_path / "j.jsonl"), first_look=True)["selection_sensitivity"]
    assert s["flipped_by_lowest_observed_fill"] is True and s["breakeven_fill_bps"] == s["lowest_observed_fill_bps"]
    assert "already not supported" in s["breakeven_note"]


# ============================================================================================ hostile review of this phase

@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), 1e9, -1e9, True, "5"])
def test_an_unusable_venue_floor_is_refused_before_the_one_shot_look_is_journaled(tmp_path, bad):
    # Reproduced: a NaN floor was journaled with the look and then crashed the report, losing the anchor of a look that cannot be repeated.
    j = tmp_path / "looks.jsonl"
    with pytest.raises(TJ.JournalError, match="venue funding floor"):
        TJ.evaluate_logged(window_prints(0.3, seed=1), 10.0, str(j), first_look=True, venue_floor_bps=bad)
    assert not j.exists()                                                        # nothing was spent


def test_the_cli_refuses_a_nan_floor_without_spending_the_look_and_prints_the_anchor_first_when_it_does_run(tmp_path, capsys):
    (tmp_path / "w").mkdir()
    f = tmp_path / "w" / "funding.jsonl"
    f.write_text("".join(json.dumps({"ts_ms": t, "funding_bps": v}) + "\n" for t, v in window_prints(0.3, seed=1)))
    j = str(tmp_path / "w" / "looks.jsonl")
    assert TJ.main(["evaluate", "--funding", str(f), "--journal", j, "--first-look", "--allow-unanchored", "--venue-funding-floor-bps", "nan"]) == 2
    assert not os.path.exists(j)
    capsys.readouterr()
    assert TJ.main(["evaluate", "--funding", str(f), "--journal", j, "--first-look", "--allow-unanchored", "--venue-funding-floor-bps", "-33.3"]) == 0
    err = capsys.readouterr().err
    assert err.lstrip().startswith("ANCHOR TO RECORD NOW")                       # the irreplaceable line comes before anything that could fail


def test_the_break_even_status_tells_the_four_situations_apart(tmp_path):
    full, cherry = _null_true_prints_with_the_lowest_dropped()
    a = TJ.evaluate_logged(full, 10.0, str(tmp_path / "a.jsonl"), first_look=True)["selection_sensitivity"]
    assert a["breakeven_status"] == "NOTHING_MISSING" and a["breakeven_fill_bps"] is None
    b = TJ.evaluate_logged(cherry, 10.0, str(tmp_path / "b.jsonl"), first_look=True)["selection_sensitivity"]
    assert b["breakeven_status"] == "ALREADY_FLIPPED_AT_LOWEST_OBSERVED"
    f2 = window_prints(2.0, seed=3)
    keep = set(sorted(f2, key=lambda x: x[1])[87:])
    c = TJ.evaluate_logged([p for p in f2 if p in keep], 10.0, str(tmp_path / "c.jsonl"), first_look=True)["selection_sensitivity"]
    assert c["breakeven_status"] == "FLIPS_AT" and c["breakeven_fill_bps"] is not None
    nosupport = window_prints(0.1, seed=3)
    keep2 = set(sorted(nosupport, key=lambda x: x[1])[87:])
    d = TJ.evaluate_logged([p for p in nosupport if p in keep2], 10.0, str(tmp_path / "d.jsonl"), first_look=True)["selection_sensitivity"]
    assert d["breakeven_status"] == "REGISTERED_VERDICT_NOT_SUPPORTED" and d["breakeven_fill_bps"] is None


@pytest.mark.parametrize("floor,ok", [(1000.0, True), (-1000.0, True), (999.999, True), (1000.0001, False), (-1000.0001, False)])
def test_the_venue_floor_bound_is_inclusive_at_exactly_1000_bps(tmp_path, floor, ok):
    j = tmp_path / "looks.jsonl"
    if ok:
        rep = TJ.evaluate_logged(window_prints(0.3, seed=1), 10.0, str(j), first_look=True, venue_floor_bps=floor)
        assert rep["journal"]["n_trials"] == 1
    else:
        with pytest.raises(TJ.JournalError, match="venue funding floor"):
            TJ.evaluate_logged(window_prints(0.3, seed=1), 10.0, str(j), first_look=True, venue_floor_bps=floor)
        assert not j.exists()


def test_the_fifth_breakeven_status_branch_and_the_keys_when_the_check_cannot_run(tmp_path, monkeypatch):
    # With realistic dispersion the STILL_SUPPORTED branch is practically unreachable (filling even a few settlements 50 bps low inflates the variance so far
    # that the registered test loses power), so its LOGIC is tested with a stubbed evaluator that always says supported. Say so rather than pretend.
    strong = window_prints(10.0, seed=3)
    keep = set(sorted(strong, key=lambda x: x[1])[87:])
    present = [p for p in strong if p in keep]
    real = CP.evaluate
    monkeypatch.setattr(CP, "evaluate", lambda *a, **k: {"verdict": "EDGE_SUPPORTED_AFTER_ADJUSTMENT"})
    out = TJ._selection_sensitivity(present, 10.0, b"", None, {"verdict": "EDGE_SUPPORTED_AFTER_ADJUSTMENT"})
    assert out["breakeven_status"] == "STILL_SUPPORTED_50_BPS_BELOW_LOWEST_OBSERVED" and out["breakeven_fill_bps"] is None
    assert out["flipped_by_lowest_observed_fill"] is False
    # and when the check itself cannot run, the keys still exist (both fallbacks)
    monkeypatch.setattr(CP, "evaluate", lambda *a, **k: (_ for _ in ()).throw(SP.DataIntegrityError("boom")))
    out = TJ._selection_sensitivity(present, 10.0, b"", None, {"verdict": "EDGE_SUPPORTED_AFTER_ADJUSTMENT"})
    assert out["breakeven_status"] == "NOT_EVALUATED" and out["breakeven_fill_bps"] is None and out["flipped_by_lowest_observed_fill"] is None
    monkeypatch.setattr(CP, "evaluate", real)
    monkeypatch.setattr(TJ, "_selection_sensitivity", lambda *a, **k: (_ for _ in ()).throw(ZeroDivisionError("boom")))
    rep = TJ.evaluate_logged(window_prints(0.3, seed=1), 10.0, str(tmp_path / "j.jsonl"), first_look=True)
    assert rep["selection_sensitivity"]["breakeven_status"] == "NOT_EVALUATED" and rep["selection_sensitivity"]["breakeven_fill_bps"] is None


# ---------------------------------------------------------------- unit screen on a hand-made file (reproduced: fractions were evaluated and journaled as a look)

@pytest.mark.parametrize("scale,refused", [(1.0, False), (1e-4, True), (1e4, True), (1e-8, True)])
def test_a_prints_file_in_the_wrong_unit_is_refused_before_a_look_is_spent(tmp_path, scale, refused):
    base = window_prints(1.0, seed=3)
    scaled = [(t, v * scale) for t, v in base]
    j = tmp_path / "j.jsonl"
    if refused:
        with pytest.raises(TJ.JournalError, match="FRACTIONS|plausible"):
            TJ.evaluate_logged(scaled, 10.0, str(j), first_look=True)
        assert not j.exists(), "a refused file must not create or extend the journal"
    else:
        TJ.evaluate_logged(scaled, 10.0, str(j), first_look=True)       # positive control: the real scale is accepted


def test_a_single_absurd_print_is_refused(tmp_path):
    base = window_prints(0.3, seed=3)
    with pytest.raises(TJ.JournalError, match="plausible"):
        TJ.evaluate_logged(base[:-1] + [(base[-1][0], 1e9)], 10.0, str(tmp_path / "j.jsonl"), first_look=True)
    with pytest.raises(TJ.JournalError):
        TJ.screen_units([(1, float("nan"))])
