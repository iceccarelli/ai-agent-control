"""Round 19: close_testnet_short --arm takes the single-writer authority (the REAL guard: marker real_single_writer). Before this, the one order-capable ``--arm`` tool without the guard could buy back
the perp short of another executor's hedge and leave its spot leg naked. Includes REAL parent/child process tests: the guarded unattended drill's child is recognised through the OS parent pid, a
stranger is refused."""
from __future__ import annotations
import os, subprocess, sys, textwrap
import pytest
HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path[:0] = [HERE, BOT, os.path.join(BOT, "tools")]
import close_testnet_short as C
import drill as D
import ms_executor_lock as XL
import legacy_executor_guard as LG
from test_close_testnet_short import FakeBroker, bot
pytestmark = pytest.mark.real_single_writer

@pytest.fixture()
def wired(monkeypatch):
    monkeypatch.setattr(D.InterruptGuard, "install", lambda self: None)
    monkeypatch.setattr(C.time, "sleep", lambda s: None)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda prompt="": C.confirmation_phrase("BTCUSDT", 0.001))
    def install(b):
        monkeypatch.setattr(D, "_load_bot", lambda p="": bot(b))
    return install

def test_armed_close_without_lease_configuration_sends_nothing(wired):
    b = FakeBroker(); wired(b)
    assert C.main(["--arm"]) == 3 and b.sent == []

def test_armed_close_is_refused_while_the_autopilot_holds_the_local_lock(wired):
    path = os.environ["MS_EXECUTOR_LOCK_PATH"]
    other = XL.ExecutorLock(path, role="autopilot", state_dir="/auto", now_ms=1); assert other.try_acquire()
    b = FakeBroker(); wired(b)
    assert C.main(["--arm"]) == 3 and b.sent == []

def test_armed_close_is_refused_when_the_lease_is_held_elsewhere(wired, monkeypatch):
    monkeypatch.setattr(LG, "acquire", lambda env, **k: (None, "LEASE_HELD_BY_ANOTHER_EXECUTOR"))
    b = FakeBroker(); wired(b)
    assert C.main(["--arm"]) == 3 and b.sent == []
    assert XL.ExecutorLock(os.environ["MS_EXECUTOR_LOCK_PATH"], role="p", state_dir="/x", now_ms=1).try_acquire()   # lock freed after refusal

def test_flat_venue_needs_no_authority(wired):
    b = FakeBroker(short=0.0); wired(b)
    assert C.main(["--arm"]) == 0 and b.sent == []

def test_with_authority_it_closes_and_releases_both(wired, monkeypatch):
    class L:
        released=False
        def release(self): self.released=True; return True
    lease = L()
    monkeypatch.setattr(LG, "acquire", lambda env, **k: (LG.Handle(lease), ""))
    b = FakeBroker(); wired(b)
    assert C.main(["--arm"]) == 0 and len(b.sent) == 1 and lease.released
    assert XL.ExecutorLock(os.environ["MS_EXECUTOR_LOCK_PATH"], role="p", state_dir="/x", now_ms=1).try_acquire()

def test_unarmed_look_needs_no_authority(wired):
    b = FakeBroker(); wired(b)
    path = os.environ["MS_EXECUTOR_LOCK_PATH"]
    other = XL.ExecutorLock(path, role="autopilot", state_dir="/auto", now_ms=1); assert other.try_acquire()
    assert C.main([]) == 0 and b.sent == []

DRIVER = textwrap.dedent('''
    import os, sys, types
    sys.path[:0] = [sys.argv[1], os.path.join(sys.argv[1], "tools"), os.path.join(sys.argv[1], "tests")]
    import close_testnet_short as C, drill as D
    from test_close_testnet_short import FakeBroker, bot
    b = FakeBroker()
    D._load_bot = lambda p="": bot(b)
    D.InterruptGuard.install = lambda self: None
    C.time.sleep = lambda s: None
    sys.stdin.isatty = lambda: True
    __builtins__.input = lambda prompt="": C.confirmation_phrase("BTCUSDT", 0.001)
    code = C.main(["--arm", "--no-prompt", "--max-qty", "0.0015"])
    print("SENT", len(b.sent)); sys.exit(code)
''')

def _child(tmp_path):
    drv = tmp_path / "drv.py"; drv.write_text(DRIVER)
    env = dict(PATH=os.environ["PATH"], HOME=str(tmp_path), MS_EXECUTOR_LOCK_PATH=os.environ["MS_EXECUTOR_LOCK_PATH"], PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run([sys.executable, str(drv), BOT], env=env, capture_output=True, text=True, timeout=60)

def test_real_child_process_recognises_its_guarded_parent(tmp_path):
    parent = XL.ExecutorLock(os.environ["MS_EXECUTOR_LOCK_PATH"], role="legacy-drill", state_dir="/run", now_ms=1)
    assert parent.try_acquire()                                    # holder note pid == THIS process == the child's real parent
    r = _child(tmp_path)
    assert r.returncode == 0 and "SENT 1" in r.stdout, (r.stdout, r.stderr)

def test_real_child_of_a_different_parent_is_refused(tmp_path):
    stranger = XL.ExecutorLock(os.environ["MS_EXECUTOR_LOCK_PATH"], role="legacy-drill", state_dir="/run", now_ms=1)
    assert stranger.try_acquire()
    # rewrite note so the holder pid is NOT the child's parent
    import json; fd = stranger._fd; os.ftruncate(fd, 0); os.lseek(fd, 0, 0)
    os.write(fd, json.dumps({"pid": os.getpid()+12345, "role": "legacy-drill", "state_dir": "/run", "started_ms": 1}).encode())
    r = _child(tmp_path)
    assert r.returncode == 3 and "REFUSED" in r.stderr, (r.stdout, r.stderr)
