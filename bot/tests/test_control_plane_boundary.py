"""The order path imports no LLM, and the Reviewer never reaches the venue.

`docs/human/AGENT_CONTROL_PLANE.md` states the role matrix; this file is the
half of it a machine can check.

Two existing tests already ban LLM names, and both do it by substring on one
file: `test_carry_wiring.py` scans a slice of `main.py`, `test_carry_risk.py`
scans `carry_risk.py`. A substring ban is defeated by an alias
(`import openai as o` is caught, `from openai import *` is caught, but
`importlib.import_module("openai")` is not), and it false-positives on prose.
So the check here is an AST walk over every module on the order path.

The Reviewer module does not exist yet. The test for it is written anyway and
FAILS CLOSED: the moment a module with that name appears, it is held to the
contract without anyone remembering to come back here.
"""
from __future__ import annotations

import ast
import json
import os

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CHARTER = os.path.join(ROOT, "docs", "human", "AGENT_CONTROL_PLANE.md")

#: Everything that can decide, size, gate or send an order.
ORDER_PATH = (
    "main.py",
    "trading_engine.py",
    "carry_engine.py",
    "carry_broker.py",
    "carry_risk.py",
    "carry_costs.py",
    "bybit_connection.py",
    "risk_management.py",
    "position_sizing.py",
    "promotion_gate.py",
    "shadow_strategy.py",
    "shadow.py",
)

#: Model vendors and local runtimes alike. Ollama is off-path for the same
#: reason Grok is: being local does not make it deterministic.
LLM_MODULES = (
    "openai", "anthropic", "claude", "grok", "xai", "ollama",
    "llama_cpp", "transformers", "langchain", "litellm", "cohere",
    "google.generativeai", "vertexai", "replicate", "huggingface_hub",
)

#: What a Reviewer must never be able to reach.
VENUE_MODULES = ("bybit_connection", "trading_engine", "carry_broker")


def imported_names(path):
    """Every module name imported by `path`, by AST. Docstrings cannot lie."""
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=path)
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def dynamic_import_targets(path):
    """String literals handed to importlib.import_module / __import__."""
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=path)
    out = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "attr", None) or getattr(func, "id", None)
        if name not in ("import_module", "__import__"):
            continue
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                out.add(arg.value)
    return out


class TestNoLlmOnTheOrderPath:
    @pytest.mark.parametrize("module", ORDER_PATH)
    def test_the_module_imports_no_model_client(self, module):
        path = os.path.join(ROOT, module)
        if not os.path.isfile(path):
            pytest.fail(f"{module} is on the order-path list but absent; "
                        f"update ORDER_PATH deliberately, do not let the ban "
                        f"silently stop covering a renamed module")
        found = imported_names(path) | dynamic_import_targets(path)
        for name in found:
            root = name.split(".")[0].lower()
            assert root not in LLM_MODULES, (
                f"{module} imports {name!r}: an LLM reached the order path")

    def test_the_list_covers_what_the_charter_claims(self):
        """A shrinking ban list is how this stops protecting anything."""
        assert len(ORDER_PATH) >= 12
        for required in ("main.py", "trading_engine.py", "carry_broker.py",
                         "carry_risk.py", "bybit_connection.py"):
            assert required in ORDER_PATH


class TestTheReviewerCannotReachTheVenue:
    """FAILS CLOSED. The Reviewer does not exist yet; the contract does."""

    CANDIDATES = ("reviewer.py", os.path.join("tools", "reviewer.py"),
                  "reviewer_verdict.py",
                  os.path.join("tools", "reviewer_verdict.py"))

    def _present(self):
        return [os.path.join(ROOT, c) for c in self.CANDIDATES
                if os.path.isfile(os.path.join(ROOT, c))]

    def test_a_reviewer_module_imports_no_venue_module(self):
        present = self._present()
        if not present:
            pytest.skip("no reviewer module yet — "
                        "test_the_absence_is_recorded_in_the_charter covers "
                        "this case and does not skip")
        for path in present:
            found = imported_names(path) | dynamic_import_targets(path)
            for name in found:
                assert name.split(".")[0] not in VENUE_MODULES, (
                    f"{os.path.basename(path)} imports {name!r}: the reviewer "
                    f"is OFF-PATH and may not reach the venue")

    def test_a_reviewer_module_places_no_orders(self):
        present = self._present()
        if not present:
            pytest.skip("no reviewer module yet — "
                        "test_the_absence_is_recorded_in_the_charter covers "
                        "this case and does not skip")
        for path in present:
            with open(path, encoding="utf-8") as handle:
                tree = ast.parse(handle.read(), filename=path)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    name = (getattr(node.func, "attr", None)
                            or getattr(node.func, "id", None) or "")
                    assert not name.startswith("place_"), (
                        f"{os.path.basename(path)} calls {name}()")
                    assert name not in ("clear_kill_switch_by_human",
                                        "set_allows_live"), name

    def test_the_absence_is_recorded_in_the_charter(self):
        """No skip. Either the module exists, or the charter says it does not."""
        body = open(CHARTER, encoding="utf-8").read()
        if self._present():
            return
        assert "MUST NOT import" in body
        for banned in VENUE_MODULES:
            assert banned in body, (
                f"the charter must name {banned} as off-limits to the reviewer")


class TestTheCharterSaysWhatItMustSay:
    def test_it_exists(self):
        assert os.path.isfile(CHARTER), CHARTER

    def test_it_carries_the_five_roles(self):
        body = open(CHARTER, encoding="utf-8").read()
        for role in ("EXECUTION BOT", "BUILDER", "REVIEWER", "LOCAL INFERENCE",
                     "ORCHESTRATOR"):
            assert role in body, role

    def test_it_pins_the_verdict_schema(self):
        body = open(CHARTER, encoding="utf-8").read()
        for key in ("allows_progress", "blockers", "stage_b",
                    "forward_n_trades", "closed_forward_bars",
                    "allows_live_must_be_false", "next_actions"):
            assert key in body, key

    def test_the_verdict_example_is_valid_json_and_fails_closed(self):
        """A schema nobody can parse is a schema nobody will honour."""
        body = open(CHARTER, encoding="utf-8").read()
        block = body.split("```json", 1)[1].split("```", 1)[0]
        doc = json.loads(block)
        assert doc["allows_progress"] is False
        assert doc["risk"]["allows_live_must_be_false"] is True
        assert set(doc) == {"allows_progress", "blockers", "stage_b", "risk",
                            "next_actions"}

    def test_openclaw_is_recorded_absent_with_its_hook_points(self):
        """ABSENT is a finding. Inventing an integration would be worse."""
        body = open(CHARTER, encoding="utf-8").read()
        assert "OpenClaw: ABSENT" in body
        assert "daily_forward_refresh.py" in body
        assert "append_closed_corpus.py" in body

    def test_it_does_not_claim_live_is_authorised(self):
        body = open(CHARTER, encoding="utf-8").read().lower()
        assert "allows_live" in body
        assert "nothing in this file authorises live trading" in body
