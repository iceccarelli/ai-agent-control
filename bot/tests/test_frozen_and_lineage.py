"""The frozen strategy is provably unchanged, the candidate cannot trade, and the
forward-evidence trail says what it is.

Nothing here claims any rule earns anything. These pin what is frozen, what is
separate and what the paper counts mean, so none of it can drift or be mistaken.
"""
import ast
import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, BOT)
sys.path.insert(0, os.path.join(BOT, "tools"))

import forward_lineage as FL  # noqa: E402
import frozen_proof as FP  # noqa: E402


def test_the_frozen_strategy_and_its_freeze_artefacts_are_byte_identical_to_the_frozen_digests():
    verdict = FP.prove()
    assert verdict["ok"] is True, json.dumps(verdict, indent=2)
    assert [f["matches_pin"] for f in verdict["files"]] == [True] * len(FP.PINNED)
    assert verdict["manifest"] and all(f["matches_manifest"] in (True, None) for f in verdict["files"])


def test_a_single_changed_byte_in_the_frozen_source_fails_the_proof(tmp_path):
    for rel in FP.PINNED:
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(os.path.join(BOT, rel), dst)
    shutil.copytree(os.path.join(BOT, "artifacts"), tmp_path / "artifacts", dirs_exist_ok=True)
    assert FP.prove(str(tmp_path))["ok"] is True
    frozen = tmp_path / "signals" / "funding_carry_fade_v1.py"
    frozen.write_bytes(frozen.read_bytes() + b"\n#")
    verdict = FP.prove(str(tmp_path))
    assert verdict["ok"] is False
    assert [f["path"] for f in verdict["files"] if not f["matches_pin"]] == ["signals/funding_carry_fade_v1.py"]


def test_the_candidate_is_separate_and_order_less():
    tree = ast.parse(open(os.path.join(BOT, "candidate_carry_depth_v1.py"), encoding="utf-8").read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    forbidden = {"bybit_connection", "carry_broker", "ledger", "persistence", "close_executor", "main",
                 "requests", "socket", "urllib", "signals", "config"}
    assert imported & forbidden == set(), imported & forbidden
    source = open(os.path.join(BOT, "candidate_carry_depth_v1.py"), encoding="utf-8").read()
    for token in ("place_market", "place_order", "order/create", "funding_carry_fade_v1"):
        code_lines = [l for l in source.splitlines() if token in l and not l.lstrip().startswith(("#", '"'))]
        assert not [l for l in code_lines if "import" in l or "(" in l], (token, code_lines)


def test_the_forward_lineage_is_one_consistent_paper_record_for_a_different_book():
    out = FL.lineage(now=time.mktime(time.strptime("2026-11-05", "%Y-%m-%d")))
    assert out["consistent"] is True
    assert out["counts"]["forward_n_trades_field"] == out["counts"]["trades_listed"] == 4
    assert out["evidence_class"] == "SIMULATED/PAPER" and out["is_cash"] is False
    assert "carry book" in out["belongs_to"]["book"]
    assert out["produced_from_dirty_tree"] is True
    assert out["gate_state"] == "STALE"
    assert out["is_stage1_evidence"] is False and out["registration_eligible"] is False
