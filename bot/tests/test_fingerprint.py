import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, BOT)

import fingerprint as FPR  # noqa: E402


def test_the_code_digest_changes_with_any_source_byte(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "b.py").write_text("y = 2\n")
    first = FPR.code_digest(str(tmp_path))
    FPR._cache.clear()
    (tmp_path / "b.py").write_text("y = 3\n")
    assert FPR.code_digest(str(tmp_path)) != first
    FPR._cache.clear()
    (tmp_path / "c.txt").write_text("not python")
    (tmp_path / "b.py").write_text("y = 3\n")
    assert FPR.code_digest(str(tmp_path)) == FPR.code_digest(str(tmp_path))


def test_the_fingerprint_carries_no_credential_shaped_field_and_no_secret():
    for name in FPR.SAFE_FIELDS:
        assert not any(w in name.upper() for w in ("KEY", "SECRET", "TOKEN", "PASSWORD", "ACK"))
    cfg = types.SimpleNamespace(BYBIT_VENUE="testnet", USE_TESTNET=True, PAPER_TRADING=True,
                                BYBIT_API_KEY="must-not-appear", BYBIT_API_SECRET="must-not-appear",
                                LIVE_TRADING_ACK="must-not-appear", BUILD_COMMIT="abc123", FLY_IMAGE_REF="")
    fp = FPR.runtime_fingerprint(cfg)
    text = repr(fp)
    assert "must-not-appear" not in text
    assert fp["build_commit"] == "abc123" and fp["image_ref"] == "unknown"
    assert fp["config"]["BYBIT_VENUE"] == "testnet" and len(fp["code_sha256"]) == 64


def test_health_reports_the_fingerprint_of_the_running_process():
    import config as C
    import main as app
    cfg = C.load({"BYBIT_VENUE": "testnet", "BUILD_COMMIT": "deadbeef"})
    fp = FPR.runtime_fingerprint(cfg)
    assert fp["build_commit"] == "deadbeef" and fp["config"]["USE_TESTNET"] is True
    assert "fingerprint" in open(os.path.join(BOT, "main.py")).read()
    assert app._fingerprint is FPR


def test_an_unreadable_code_directory_does_not_break_the_fingerprint(tmp_path):
    cfg = types.SimpleNamespace(BYBIT_VENUE="testnet", USE_TESTNET=True, PAPER_TRADING=True)
    fp = FPR.runtime_fingerprint(cfg, root=str(tmp_path / "does-not-exist"))
    assert fp["code_sha256"] == "unavailable"
