"""One LOCAL writer per account: the kernel lock that keeps execute, reconcile and the emergency unwind from ever being two writers on one journal, without ever blocking the exit.
Also: the manual execute's lease is RENEWED while it runs (a lapsed lease would let the autopilot open a second hedge) and a lost lease engages the kill switch."""
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path[:0] = [HERE, BOT, os.path.join(BOT, "tools")]

import ms_executor_lock as XL  # noqa: E402
import ms_testnet_adapter as AD  # noqa: E402
import ms_testnet_session as S  # noqa: E402
from ms_testnet_world import World, begin  # noqa: E402
from test_ms_testnet_executor import drive_full  # noqa: E402

pytestmark = pytest.mark.real_single_writer      # these tests exercise the REAL single-writer guard of the armed drill
CREATE = "/v5/order/create"


@pytest.fixture
def w(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def lock_for(w, role="autopilot", state_dir=None):
    return XL.ExecutorLock(XL.lock_path(AD.DEFAULT_CAPABILITY_PATH), role=role, state_dir=state_dir or w.state_dir, now_ms=w.clock.now())


def ready(w):
    assert w.run("preflight") == 0 and w.run("plan", "--symbol", "BTCUSDT", "--notional-usd", "90") == 0
    return w.plan_doc()["plan_digest"]


# ------------------------------------------------------------------ the lock itself
def test_one_holder_at_a_time_a_note_says_who_and_release_frees_it(w):
    a, b = lock_for(w, "execute", "/tmp/x"), lock_for(w, "unwind", "/tmp/y")
    assert a.try_acquire() and a.held and not b.try_acquire()
    h = XL.read_holder(a.path)
    assert h["role"] == "execute" and h["state_dir"] == "/tmp/x" and h["pid"] == os.getpid()
    a.release()
    assert XL.read_holder(a.path) is None and b.try_acquire()
    b.release()


def test_a_malformed_note_is_an_unknown_holder_and_a_symlinked_lock_file_is_refused(w, tmp_path):
    a = lock_for(w)
    open(a.path, "w").write("{not json")
    assert XL.read_holder(a.path) is None
    os.unlink(a.path)
    os.symlink(str(tmp_path / "elsewhere"), a.path)
    assert a.try_acquire() is False                                   # O_NOFOLLOW: a planted link is never followed, and no lock is claimed


def test_the_lock_is_dropped_when_the_holder_dies_without_cleanup(w):
    a = lock_for(w)
    assert a.try_acquire()
    os.close(a._fd)                                                    # what the kernel does at process death
    a._fd = None
    assert lock_for(w, "unwind").try_acquire()


# ------------------------------------------------------------------ execute and reconcile respect it
def test_execute_is_refused_while_another_session_holds_the_lock_and_sends_nothing(w):
    d = ready(w)
    holder = lock_for(w, "autopilot", "/tmp/auto")
    assert holder.try_acquire()
    before = len(w.requests_to(CREATE))
    assert w.run("execute", "--confirm-plan-digest", d, typed=d) == S.EXIT_REFUSED
    assert "account lock" in w.text() and "autopilot" in w.text() and len(w.requests_to(CREATE)) == before == 0
    holder.release()
    assert w.run("execute", "--confirm-plan-digest", d, typed=d) == 0 and w.record()["flat_verified"] is True


def test_execute_holds_the_lock_for_the_whole_session_and_frees_it_after(w):
    d = ready(w)
    seen = []

    def pre(*_a):
        seen.append(lock_for(w, "probe").try_acquire())                 # an entry order is about to leave: nobody else may be able to take the lock
        return None
    a = S._parser().parse_args(["--state-dir", w.state_dir, "execute", "--confirm-plan-digest", d])
    rc = S.main(["--state-dir", w.state_dir, "execute", "--confirm-plan-digest", d], env=w.env, inner_transport=w.fake, clock_ms=w.clock.now, sleep=w.clock.sleep,
                input_fn=lambda _p: d, out=w.out.append, pre_order_check=pre)
    assert rc == 0 and seen and not any(seen)
    assert lock_for(w, "after").try_acquire()                           # released by finally


def test_a_plain_reconcile_is_refused_while_a_session_holds_the_lock(w):
    ready(w)
    run, ctx, rec = begin(w)
    holder = lock_for(w, "autopilot", "/tmp/auto")
    assert holder.try_acquire()
    before = len(w.fake.requests)
    assert w.run("reconcile") == S.EXIT_REFUSED and "must not interleave" in w.text()
    assert len(w.fake.requests) == before                               # nothing was even read from the venue


# ------------------------------------------------------------------ the emergency unwind hands over, never runs a second writer
def _session_dir(path):
    os.makedirs(path, exist_ok=True)
    open(os.path.join(path, "engine_journal.jsonl"), "a").close()                         # what a real session directory holds
    return path


def _holder_that_releases_when_killed(w, holder_dir):
    holder = lock_for(w, "execute", holder_dir)
    assert holder.try_acquire()
    real_sleep = w.clock.sleep

    def sleep(s):
        real_sleep(s)
        if os.path.exists(os.path.join(holder_dir, "MS_KILL")):          # the holder's own loop sees its kill switch, unwinds, and finishes
            holder.release()
    w.clock.sleep = sleep
    return holder


def test_the_unwind_engages_the_holders_kill_switch_waits_and_then_verifies_flat(w, tmp_path):
    d = ready(w)
    run, ctx, rec = begin(w)
    assert drive_full(w, run, 2) == "HEDGED"
    holder_dir = _session_dir(str(tmp_path / "holder_session"))
    holder = _holder_that_releases_when_killed(w, holder_dir)
    rc = w.run("reconcile", "--unwind", "--confirm-plan-digest", d)
    assert rc == 0, w.text()
    assert os.path.exists(os.path.join(holder_dir, "MS_KILL")) and "kill switch was engaged" in w.text() and "the holder has finished" in w.text()
    assert open(os.path.join(holder_dir, S.FILES["owner_unwind"])).read() == S.OWNER_UNWIND_KILL_REASON      # the autopilot reads this to stop instead of re-arming
    assert w.record()["flat_verified"] is True and all(p["size"] == 0 for p in w.fake.positions.values())
    assert not holder.held


def test_an_unwind_whose_holder_never_lets_go_exits_nonzero_and_sends_nothing(w, tmp_path):
    d = ready(w)
    run, ctx, rec = begin(w)
    assert drive_full(w, run, 2) == "HEDGED"
    holder_dir = _session_dir(str(tmp_path / "stuck"))
    assert lock_for(w, "execute", holder_dir).try_acquire()
    creates = len(w.requests_to(CREATE))
    assert w.run("reconcile", "--unwind", "--confirm-plan-digest", d) == S.EXIT_FAIL
    assert "TIMED OUT" in w.text() and len(w.requests_to(CREATE)) == creates                # never a second writer
    assert any(p["size"] != 0 for p in w.fake.positions.values())                            # and it did not pretend the hedge was closed


def test_an_unwind_with_an_unidentifiable_holder_does_not_guess(w):
    d = ready(w)
    run, ctx, rec = begin(w)
    holder = lock_for(w)
    assert holder.try_acquire()
    os.ftruncate(holder._fd, 0)                                                              # the note is gone: who holds it is unknown
    creates = len(w.requests_to(CREATE))
    assert w.run("reconcile", "--unwind", "--confirm-plan-digest", d) == S.EXIT_FAIL
    assert "cannot be identified" in w.text() and len(w.requests_to(CREATE)) == creates


def test_the_unwind_still_needs_the_digest_even_when_it_would_hand_over(w):
    ready(w)
    begin(w)
    assert lock_for(w).try_acquire()
    assert w.run("reconcile", "--unwind", "--confirm-plan-digest", "wrong") == S.EXIT_REFUSED and "UNWIND_NEEDS" in w.text()


# ------------------------------------------------------------------ a halted group no longer strands the emergency exit
def test_a_group_halted_by_REAL_exit_rejections_is_unwound_by_the_confirmed_owner_command(w):
    """Review finding: the clear used to be a no-op against a genuine halt (the rejected-exit COUNT was still >= the limit, so the very next step re-halted and sent nothing). The venue really rejects
    every exit here, the engine really halts, and the confirmed unwind must still end flat."""
    d = ready(w)
    run, ctx, rec = begin(w)
    assert drive_full(w, run, 2) == "HEDGED"
    w.fake.reject[""] = (10001, "params error")                                                    # every order (so every EXIT) is refused by the venue
    run.engine.request_exit(run.gid, w.clock.now(), "TEST_EXIT")
    for _ in range(40):
        w.clock.sleep(3)
        now = w.clock.now()
        run.engine.step(now, market_ts_ms=now, account_ts_ms=now)
        run.bookkeep()
        if run.engine.groups[run.gid].halted_reason:
            break
    g = run.engine.groups[run.gid]
    assert g.halted_reason == "EXIT_ORDERS_REJECTED" and sum(1 for l in g.exit_legs() if l.status == "REJECTED") >= 5
    assert any(p["size"] != 0 for p in w.fake.positions.values())                                   # the hedge is still open
    w.fake.reject.clear()                                                                           # the owner fixed whatever the venue disliked
    rc = w.run("reconcile", "--unwind", "--confirm-plan-digest", d)
    assert rc == 0, w.text()
    assert "lifted by the confirmed unwind" in w.text() and w.record()["flat_verified"] is True and all(p["size"] == 0 for p in w.fake.positions.values())
    lines = [json.loads(l) for l in open(os.path.join(w.state_dir, "engine_journal.jsonl"))]
    lifted = [e for e in lines if e["type"] == "HALT_GROUP" and e["body"]["reason"] == ""]
    assert lifted and lifted[-1]["body"]["exit_floor"] >= 5                                          # the chain shows who lifted it and from which rejection count


def test_after_the_lift_exits_are_really_tried_again_and_only_NEW_rejections_count(w):
    ready(w)
    run, ctx, rec = begin(w)
    assert drive_full(w, run, 2) == "HEDGED"
    w.fake.reject[""] = (10001, "params error")
    run.engine.request_exit(run.gid, w.clock.now(), "TEST_EXIT")
    for _ in range(40):
        w.clock.sleep(3)
        now = w.clock.now()
        run.engine.step(now, market_ts_ms=now, account_ts_ms=now)
        run.bookkeep()
        if run.engine.groups[run.gid].halted_reason:
            break
    g = run.engine.groups[run.gid]
    n = sum(1 for l in g.exit_legs() if l.status == "REJECTED")
    assert run.engine.clear_exit_halt(run.gid, w.clock.now()) == "EXIT_ORDERS_REJECTED" and g.exit_attempt_floor == n
    m = sum(1 for l in g.exit_legs() if l.status == "REJECTED")
    assert m - n == n >= run.engine.policy.max_exit_attempts, (n, m)                               # a FRESH budget: as many new attempts as the first halt allowed (the old count no longer re-halts at once)
    assert g.halted_reason == "EXIT_ORDERS_REJECTED"                                               # and a venue that still refuses everything halts again, with the floor moved up
    assert run.engine.clear_exit_halt("no-such-group", w.clock.now()) is None


def test_only_exit_halts_are_lifted_a_conflicting_order_id_halt_stays(w):
    ready(w)
    run, ctx, rec = begin(w)
    assert drive_full(w, run, 2) == "HEDGED"
    now = w.clock.now()
    run.engine._emit("HALT_GROUP", run.gid, now, {"reason": "CONFLICTING_ORDER_ID"})
    assert run.engine.clear_exit_halt(run.gid, now) is None and run.engine.groups[run.gid].halted_reason == "CONFLICTING_ORDER_ID"
    assert run.engine.clear_exit_halt("nope", now) is None


# ------------------------------------------------------------------ the manual execute keeps its lease alive and a lost lease unwinds
class FakeHandle:
    def __init__(self, renewals):
        self.renewals, self.calls, self.released = list(renewals), 0, False

    def renew(self):
        self.calls += 1
        return self.renewals.pop(0) if self.renewals else True

    def release(self):
        self.released = True
        return True


def test_a_long_manual_run_renews_its_lease_and_releases_it_at_the_end(tmp_path, capsys):
    h = FakeHandle([True, True, True])
    t = {"now": 0.0}

    def run(argv, env, sleep=None, pre_order_check=None):
        for _ in range(4):
            t["now"] += 31.0
            sleep(1.0)
        return 0
    rc = S.guarded_main(["--state-dir", str(tmp_path / "s"), "execute"], env={}, acquire=lambda e: (h, ""), run=run, monotonic=lambda: t["now"], real_sleep=lambda s: None)
    assert rc == 0 and h.calls == 4 and h.released is True and not os.path.exists(str(tmp_path / "s" / "MS_KILL"))


def test_a_lease_that_cannot_be_renewed_engages_the_kill_switch_once_and_the_run_unwinds(tmp_path, capsys):
    h = FakeHandle([True, False, False])
    t = {"now": 0.0}
    os.makedirs(str(tmp_path / "s"))

    def run(argv, env, sleep=None, pre_order_check=None):
        for _ in range(4):
            t["now"] += 31.0
            sleep(1.0)
        return 0
    S.guarded_main(["--state-dir", str(tmp_path / "s"), "execute"], env={}, acquire=lambda e: (h, ""), run=run, monotonic=lambda: t["now"], real_sleep=lambda s: None)
    assert os.path.exists(str(tmp_path / "s" / "MS_KILL")) and capsys.readouterr().out.count("EXECUTOR LEASE LOST") == 1 and h.released is True


# ------------------------------------------------------------------ the legacy armed drill takes the same single-writer authority
import drill as DR  # noqa: E402
import legacy_executor_guard as LG  # noqa: E402
import unattended_drill as UD  # noqa: E402


class _Lease:
    def __init__(self):
        self.released = False

    def release(self):
        self.released = True
        return True


@pytest.fixture
def lockpath(tmp_path, monkeypatch):
    p = str(tmp_path / "locks" / "ms_executor.lock")
    monkeypatch.setenv("MS_EXECUTOR_LOCK_PATH", p)
    return p


def test_an_armed_drill_without_a_lease_configuration_places_nothing_and_holds_no_lock(lockpath):
    release, why = DR.single_writer_guard({})
    assert release is None and "NO_GLOBAL_LEASE_CONFIGURED" in why
    assert XL.ExecutorLock(lockpath, role="probe", state_dir="/x", now_ms=1).try_acquire()           # the local lock was released after the refusal


def test_an_armed_drill_takes_both_the_local_lock_and_the_lease_and_frees_both(lockpath, monkeypatch):
    lease = _Lease()
    monkeypatch.setattr(LG, "acquire", lambda env, **k: (LG.Handle(lease), ""))
    release, why = DR.single_writer_guard({})
    assert release is not None and XL.read_holder(lockpath)["role"] == "legacy-armed"
    assert not XL.ExecutorLock(lockpath, role="probe", state_dir="/x", now_ms=1).try_acquire()
    release()
    assert lease.released and XL.ExecutorLock(lockpath, role="probe", state_dir="/x", now_ms=1).try_acquire()


def test_an_armed_drill_is_refused_while_any_other_session_holds_the_lock(lockpath, monkeypatch):
    monkeypatch.setattr(LG, "acquire", lambda env, **k: (_ for _ in ()).throw(AssertionError("must not even ask for the lease")))
    other = XL.ExecutorLock(lockpath, role="autopilot", state_dir="/auto", now_ms=1)
    assert other.try_acquire()
    release, why = DR.single_writer_guard({})
    assert release is None and "account lock" in why and "autopilot" in why


def test_an_armed_drill_started_by_the_guarded_parent_recognises_its_parent_and_nobody_else(lockpath, monkeypatch):
    parent = XL.ExecutorLock(lockpath, role="legacy-drill", state_dir="/run", now_ms=1)
    assert parent.try_acquire()
    monkeypatch.setattr(os, "getppid", lambda: os.getpid())                  # the holder's pid IS this child's parent
    release, why = DR.single_writer_guard({})
    assert release is not None and why == ""
    monkeypatch.setattr(os, "getppid", lambda: os.getpid() + 1)             # a different parent: the lock holder is a stranger
    release, why = DR.single_writer_guard({})
    assert release is None
    parent.release()
    impostor = XL.ExecutorLock(lockpath, role="autopilot", state_dir="/auto", now_ms=1)
    assert impostor.try_acquire()
    monkeypatch.setattr(os, "getppid", lambda: os.getpid())                  # right pid, wrong role: not the guarded drill
    assert DR.single_writer_guard({})[0] is None


def test_the_guarded_parent_holds_the_local_lock_while_its_children_run_and_hands_them_the_path(lockpath, monkeypatch, tmp_path):
    lease, seen = _Lease(), {}

    def run_once(run_dir, env, **kw):
        seen["holder"] = XL.read_holder(lockpath)
        seen["path"] = env.get("MS_EXECUTOR_LOCK_PATH")
        return 0
    monkeypatch.setattr(UD, "run_once", run_once)
    assert UD.guarded_run_once(str(tmp_path / "r"), env={}, acquire=lambda e: (LG.Handle(lease), "")) == 0
    assert seen["holder"]["role"] == "legacy-drill" and seen["path"] == lockpath
    assert lease.released and XL.ExecutorLock(lockpath, role="probe", state_dir="/x", now_ms=1).try_acquire()


def test_the_guarded_parent_refuses_when_the_local_lock_is_held_and_gives_the_lease_back(lockpath, monkeypatch, tmp_path):
    lease = _Lease()
    monkeypatch.setattr(UD, "run_once", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")))
    other = XL.ExecutorLock(lockpath, role="execute", state_dir="/s", now_ms=1)
    assert other.try_acquire()
    assert UD.guarded_run_once(str(tmp_path / "r"), env={}, acquire=lambda e: (LG.Handle(lease), "")) == UD.EXIT_REFUSED and lease.released


def _guarded_execute(w, d, acquire):
    run = lambda argv, env, sleep=None, pre_order_check=None: S.main(argv, env=env, inner_transport=w.fake, clock_ms=w.clock.now, sleep=w.clock.sleep, input_fn=lambda _p: d,   # noqa: E731
                                                                      out=w.out.append, pre_order_check=pre_order_check)
    return S.guarded_main(["--state-dir", w.state_dir, "execute", "--confirm-plan-digest", d], env=w.env, acquire=acquire, run=run, monotonic=lambda: 0.0, real_sleep=lambda s: None)


def test_a_lease_that_lapsed_during_the_prompt_is_retaken_before_the_entries_when_nobody_else_holds_it(w):
    d = ready(w)
    first, second = FakeHandle([False]), FakeHandle([True] * 20)                       # the lease taken at start has expired by the time the first entry is about to leave
    handles = iter([first, second])
    assert _guarded_execute(w, d, lambda e: (next(handles), "")) == 0
    assert w.record()["flat_verified"] is True and len(w.requests_to(CREATE)) >= 2
    assert first.calls == 1 and second.released is True                                # the re-taken lease is the one released at the end


def test_a_lease_that_lapsed_and_was_taken_by_another_executor_refuses_every_entry(w):
    d = ready(w)
    answers = iter([(FakeHandle([False]), ""), (None, "LEASE_HELD_BY_ANOTHER_EXECUTOR")])
    rc = _guarded_execute(w, d, lambda e: next(answers))
    assert len(w.requests_to(CREATE)) == 0, "an ENTRY order was sent although the lease was lost to another executor"
    assert all(p["size"] == 0 for p in w.fake.positions.values())


# ------------------------------------------------------------------ review round: the emergency unwind must not be refusable by lock plumbing
def _hung_hedge(w):
    d = ready(w)
    run, ctx, rec = begin(w)
    assert drive_full(w, run, 2) == "HEDGED"
    return d


def test_a_broken_lock_file_is_not_a_holder_the_unwind_proceeds_and_a_plain_reconcile_is_refused(w):
    d = _hung_hedge(w)
    path = XL.lock_path(AD.DEFAULT_CAPABILITY_PATH)
    os.makedirs(path)                                                                       # a DIRECTORY where the lock file should be: open() fails with EISDIR
    assert w.run("reconcile") == S.EXIT_REFUSED and "cannot be used" in w.text()
    assert w.run("reconcile", "--unwind", "--confirm-plan-digest", d) == 0, w.text()
    assert "WARNING: the account lock cannot be used" in w.text() and w.record()["flat_verified"] is True and all(p["size"] == 0 for p in w.fake.positions.values())


def test_a_lock_error_is_distinguished_from_contention(w):
    path = XL.lock_path(AD.DEFAULT_CAPABILITY_PATH)
    os.makedirs(path)
    a = lock_for(w)
    assert not a.try_acquire() and a.error and a.error.startswith("LOCK_FILE_UNUSABLE")
    os.rmdir(path)
    b1, b2 = lock_for(w, "x", "/s"), lock_for(w, "y", "/t")
    assert b1.try_acquire() and not b2.try_acquire() and b2.error is None                    # contention: no error text


def test_a_holder_that_released_between_our_attempt_and_our_look_is_retried_not_abandoned(w, monkeypatch):
    d = _hung_hedge(w)
    holder = lock_for(w, "execute", "/nowhere")
    assert holder.try_acquire()
    calls = {"n": 0}
    real_read = XL.read_holder

    def blank_then_free(path):
        calls["n"] += 1
        if calls["n"] == 1:
            holder.release()                                                                # the holder lets go right after our failed attempt; its note is blank
            return None
        return real_read(path)
    monkeypatch.setattr(XL, "read_holder", blank_then_free)
    assert w.run("reconcile", "--unwind", "--confirm-plan-digest", d) == 0, w.text()
    assert "had just finished" in w.text() and w.record()["flat_verified"] is True


def test_a_legacy_holder_never_gets_a_kill_file_and_the_unwind_says_who_to_stop(w, tmp_path):
    d = _hung_hedge(w)
    legacy_dir = str(tmp_path / "cwd_of_a_legacy_tool")
    os.makedirs(legacy_dir)
    assert lock_for(w, "legacy-armed", legacy_dir).try_acquire()
    creates = len(w.requests_to(CREATE))
    assert w.run("reconcile", "--unwind", "--confirm-plan-digest", d) == S.EXIT_FAIL
    assert "does not read a kill switch" in w.text() and "kill " in w.text()
    assert os.listdir(legacy_dir) == [] and len(w.requests_to(CREATE)) == creates              # no stray MS_KILL, nothing sent


@pytest.mark.parametrize("bad_dir", ["relative/dir", "/definitely/not/a/dir", "/tmp"])
def test_a_note_naming_a_directory_that_is_not_a_session_dir_is_not_followed(w, tmp_path, bad_dir):
    d = _hung_hedge(w)
    holder = XL.ExecutorLock(XL.lock_path(AD.DEFAULT_CAPABILITY_PATH), role="execute", state_dir=bad_dir, now_ms=1)
    assert holder.try_acquire()
    before = set(os.listdir("/tmp")) if bad_dir == "/tmp" else None
    assert w.run("reconcile", "--unwind", "--confirm-plan-digest", d) == S.EXIT_FAIL and "not an absolute path to a session state directory" in w.text()
    assert not os.path.exists(os.path.join(bad_dir, "MS_KILL")) if os.path.isabs(bad_dir) else not os.path.exists("relative")
    if before is not None:
        assert "MS_KILL" not in set(os.listdir("/tmp")) - before


@pytest.mark.parametrize("change", ["journal", "kill"])
def test_state_that_changes_during_the_typed_prompt_is_rechecked_after_the_lock(w, change):
    """Review finding: the gates ran before the (possibly minutes-long) typed prompt and the lock; a session that completed, or a kill switch engaged, in between was not seen."""
    d = ready(w)

    def prompt(_p):
        if change == "journal":
            open(os.path.join(w.state_dir, "engine_journal.jsonl"), "w").write("another process ran a session here\n")
        else:
            AD.engage_kill_switch(os.path.join(w.state_dir, "MS_KILL"), "owner stop during the prompt")
        return d
    before = S.entries_today(w.clock.now())
    rc = S.main(["--state-dir", w.state_dir, "execute", "--confirm-plan-digest", d], env=w.env, inner_transport=w.fake, clock_ms=w.clock.now, sleep=w.clock.sleep, input_fn=prompt, out=w.out.append)
    assert rc == S.EXIT_REFUSED and ("SESSION_ALREADY_EXECUTED_USE_RECONCILE" if change == "journal" else "KILL_SWITCH_ENGAGED") in w.text()
    assert not w.requests_to(CREATE) and S.entries_today(w.clock.now()) == before          # nothing sent and no entry counted
    assert lock_for(w, "after").try_acquire()                                              # and the lock was released
