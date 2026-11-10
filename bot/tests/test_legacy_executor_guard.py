"""The single-writer guard for the legacy order-capable drill: it takes the SAME server-side lease as the autopilot, fails closed, and never leaks the lease. Offline: a fake lease server, no network."""
import ast
import os
import sys

import pytest

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [BOT, os.path.join(BOT, "tools")]
import exec_evidence as XE  # noqa: E402
import legacy_executor_guard as LG  # noqa: E402
import unattended_drill as UD  # noqa: E402

OWNER = "11111111-2222-3333-4444-555555555555"
ENV = {"SUPABASE_URL": "https://abcdefgh.supabase.co", "SUPABASE_SERVICE_ROLE_KEY": "k" * 30, "SUPABASE_OWNER_ID": OWNER, "FLY_MACHINE_ID": "m1"}


class FakeServer:
    """The server side of exec_lease_*: one holder, a growing fencing token, expiry on the SERVER clock."""

    def __init__(self):
        self.holder, self.token, self.expires, self.now = None, 0, 0.0, 0.0

    def rpc(self, function, args, retries=None):
        a = args
        if function == "exec_lease_acquire":
            if self.holder not in (None, a["p_holder"]) and self.expires > self.now:
                return None
            self.token += 1
            self.holder, self.expires = a["p_holder"], self.now + a["p_ttl_seconds"]
            return self.token
        if function == "exec_lease_renew":
            if self.holder != a["p_holder"] or self.token != a["p_token"] or self.expires <= self.now:
                return False
            self.expires = self.now + a["p_ttl_seconds"]
            return True
        if function == "exec_lease_release":
            if self.holder != a["p_holder"] or self.token != a["p_token"]:
                return False
            self.holder = None
            return True
        raise AssertionError(function)


def factory_for(server):
    return lambda url, key, owner, holder: XE.LeaseClient(server, owner, holder, ttl_seconds=LG.TTL_SECONDS)       # the REAL LeaseClient code against the fake server


def test_without_supabase_configuration_the_legacy_path_places_nothing():
    for env in ({}, {"SUPABASE_URL": ENV["SUPABASE_URL"]}, {k: v for k, v in ENV.items() if k != "SUPABASE_OWNER_ID"}):
        h, why = LG.acquire(env)
        assert h is None and why.startswith("NO_GLOBAL_LEASE_CONFIGURED")


def test_the_lease_is_taken_held_for_one_run_and_released():
    srv = FakeServer()
    h, why = LG.acquire(ENV, lease_factory=factory_for(srv))
    assert h is not None and why == "" and srv.holder.startswith("legacy-drill:m1:") and h.token == 1
    assert h.release() is True and srv.holder is None and h.release() is False                     # a second release is a no-op, never an error


def test_the_autopilot_and_the_legacy_drill_can_never_hold_the_lease_together():
    srv = FakeServer()
    autopilot = XE.LeaseClient(srv, OWNER, "autopilot-machine:abc12345", ttl_seconds=120)
    assert autopilot.acquire() is True
    h, why = LG.acquire(ENV, lease_factory=factory_for(srv))
    assert h is None and why == "LEASE_HELD_BY_ANOTHER_EXECUTOR"                                    # the autopilot holds it: the legacy run places nothing
    autopilot.release()
    h, why = LG.acquire(ENV, lease_factory=factory_for(srv))
    assert h is not None
    other = XE.LeaseClient(srv, OWNER, "autopilot-machine:abc12345", ttl_seconds=120)
    assert other.acquire() is False                                                                  # and while the legacy run holds it, the autopilot cannot enter
    h.release()
    assert other.acquire() is True


def test_a_crashed_legacy_run_frees_the_lease_by_expiry_not_forever():
    srv = FakeServer()
    h, _ = LG.acquire(ENV, lease_factory=factory_for(srv))
    assert h is not None                                                                              # the process dies here: no release
    other = XE.LeaseClient(srv, OWNER, "autopilot-machine:abc12345", ttl_seconds=120)
    assert other.acquire() is False
    srv.now += LG.TTL_SECONDS + 1
    assert other.acquire() is True


def test_an_unanswerable_lease_backend_is_a_refusal():
    def boom(*a):
        raise RuntimeError("supabase unreachable")
    h, why = LG.acquire(ENV, lease_factory=boom)
    assert h is None and why == "LEASE_UNAVAILABLE:RuntimeError"
    h, why = LG.acquire({**ENV, "SUPABASE_URL": "http://evil.example.com"})                         # the real client refuses a non-https / non-supabase host
    assert h is None and why.startswith("LEASE_UNAVAILABLE")


def test_a_failing_release_never_masks_the_run_and_is_freed_by_expiry():
    class BadRelease:
        token = 5

        def acquire(self):
            return True

        def release(self):
            raise RuntimeError("release failed")
    h, _ = LG.acquire(ENV, lease_factory=lambda *a: BadRelease())
    assert h.release() is False


# ---------------------------------------------------------------- the order-capable entry point
def test_a_refused_lease_means_run_once_is_never_called(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(UD, "run_once", lambda *a, **k: called.append(1) or 0)
    code = UD.guarded_run_once(str(tmp_path / "run-1"), env={}, acquire=LG.acquire)
    assert code == UD.EXIT_REFUSED and called == []
    log = open(os.path.join(str(tmp_path / "run-1"), "orchestrator.log"), encoding="utf-8").read()
    assert "refusing to place any order" in log and "NO_GLOBAL_LEASE_CONFIGURED" in log


def test_a_granted_lease_runs_once_and_is_always_released(tmp_path, monkeypatch):
    srv = FakeServer()
    seen = {}

    def fake_run(run_dir, env, **kw):
        seen["holder"] = srv.holder
        return 7
    monkeypatch.setattr(UD, "run_once", fake_run)
    acq = lambda env: LG.acquire(env, lease_factory=factory_for(srv))                                # noqa: E731
    assert UD.guarded_run_once(str(tmp_path / "r"), env=ENV, acquire=acq) == 7
    assert seen["holder"].startswith("legacy-drill:") and srv.holder is None                          # held DURING the run, free after
    for exc in (UD.Shutdown("SIGTERM"), RuntimeError("boom")):
        monkeypatch.setattr(UD, "run_once", lambda *a, _e=exc, **k: (_ for _ in ()).throw(_e))
        with pytest.raises(type(exc)):
            UD.guarded_run_once(str(tmp_path / "r2"), env=ENV, acquire=acq)
        assert srv.holder is None, "the lease leaked after an exception"


def test_the_cli_uses_the_guarded_entry_point_in_single_and_loop_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("BYBIT_API_KEY", "x" * 8)
    monkeypatch.setenv("BYBIT_API_SECRET", "y" * 8)
    for v in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_OWNER_ID"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(UD, "run_once", lambda *a, **k: (_ for _ in ()).throw(AssertionError("an order-capable run started without the lease")))
    monkeypatch.setattr(UD, "install_signal_handlers", lambda: None)
    assert UD.main(["--conformance", "--run-dir", str(tmp_path / "one")]) == UD.EXIT_REFUSED          # single run, no lease: nothing sent
    got = {}
    monkeypatch.setattr(UD, "loop", lambda root, env, max_runs=0, once=None, **k: got.setdefault("once", once) and 0)
    UD.main(["--conformance", "--loop", "--run-dir", str(tmp_path / "root")])
    assert got["once"] is UD.guarded_run_once                                                          # the loop's per-run entry point is the guarded one


def test_the_guard_module_can_reach_no_exchange_by_construction():
    src = open(os.path.join(BOT, "tools", "legacy_executor_guard.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    top = {n.names[0].name.split(".")[0] for n in tree.body if isinstance(n, ast.Import)} | {n.module.split(".")[0] for n in tree.body if isinstance(n, ast.ImportFrom) and n.module}
    assert top <= {"__future__", "uuid", "typing"}, top                                              # exec_evidence is imported lazily, inside acquire()
    low = src.lower()
    assert "bybit" not in low.replace("order-capable", "") or "api.bybit" not in low
    assert "place_order" not in low and "environ" not in low


# ---- the owner's manual `ms_testnet_session.py execute` takes the same lease (it places Testnet orders)
import ms_testnet_session as MTS  # noqa: E402


def _acquirer(server):
    return lambda env: LG.acquire(env, lease_factory=factory_for(server))


def test_manual_execute_without_a_lease_configuration_places_nothing(capsys):
    called = []
    rc = MTS.guarded_main(["execute"], env={}, run=lambda *a, **k: called.append(1) or 0)
    assert rc == MTS.EXIT_REFUSED and not called
    assert "REFUSED" in capsys.readouterr().out


def test_manual_execute_is_refused_while_the_autopilot_holds_the_lease_and_runs_under_it_otherwise():
    server = FakeServer()
    autopilot = XE.LeaseClient(server, OWNER, "autopilot:m1", ttl_seconds=3600)
    assert autopilot.acquire()
    called = []
    assert MTS.guarded_main(["execute"], env=ENV, acquire=_acquirer(server), run=lambda *a, **k: called.append(1) or 0) == MTS.EXIT_REFUSED and not called
    autopilot.release()
    seen = []

    def run(argv, env):
        seen.append(server.holder)                                  # the lease is HELD while the order-capable run executes
        return 0
    assert MTS.guarded_main(["execute"], env=ENV, acquire=_acquirer(server), run=run) == 0
    assert seen and seen[0] is not None and server.holder is None   # ... and released afterwards


def test_manual_execute_releases_the_lease_even_when_the_run_raises():
    server = FakeServer()

    def boom(argv, env):
        raise RuntimeError("venue exploded")
    with pytest.raises(RuntimeError):
        MTS.guarded_main(["execute"], env=ENV, acquire=_acquirer(server), run=boom)
    assert server.holder is None


def test_read_only_session_commands_do_not_touch_the_lease():
    def must_not_acquire(env):
        raise AssertionError("a read-only command must not take the executor lease")
    for cmd in ("preflight", "plan --symbol BTCUSDT --notional-usd 100", "assess", "status", "reconcile"):
        assert MTS.guarded_main(cmd.split(), env={}, acquire=must_not_acquire, run=lambda *a, **k: 0) == 0
