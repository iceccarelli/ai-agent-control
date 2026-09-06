"""Health bind address, decision-journal retention, dependency pins."""
from __future__ import annotations

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import config  # noqa: E402
from persistence import StateStore  # noqa: E402


def test_health_binds_loopback_by_default():
    cfg = config.load({})
    assert cfg.HEALTHCHECK_HOST == "127.0.0.1"
    assert config.load({"HEALTHCHECK_HOST": "0.0.0.0"}).HEALTHCHECK_HOST == "0.0.0.0"


def test_decisions_retention_is_bounded_and_prunes(tmp_path):
    cfg = config.load({})
    assert 1 <= cfg.DECISIONS_RETENTION_DAYS <= 3650
    store = StateStore(str(tmp_path / "s.db"))
    store.claim_writer()
    store.journal("BTCUSDT", "BLOCK", "OLD", {})
    store._exec("UPDATE decisions SET ts_epoch = ?", (time.time() - 400 * 86400,))
    store.journal("BTCUSDT", "BLOCK", "NEW", {})
    assert store.prune_decisions(older_than_days=90) == 1
    assert len(store._query("SELECT * FROM decisions")) == 1


def test_constraints_pin_every_runtime_requirement():
    req = open(os.path.join(ROOT, "requirements.txt")).read()
    con = open(os.path.join(ROOT, "constraints.txt")).read()
    for name in ("requests", "numpy", "scipy", "scikit-learn"):
        assert name in req and f"{name}==" in con
    assert "-c constraints.txt" in open(os.path.join(ROOT, "Dockerfile")).read()
