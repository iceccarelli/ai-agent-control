"""What the market-data pipeline is NOT allowed to be, pinned statically; and the
operator CLI. The pipeline is public and read-only: no credentials, no private
channel, no order path, no write outside the store directory it was given."""
from __future__ import annotations

import ast
import io
import json
import os
import re
import sys
from contextlib import redirect_stdout

import pytest

import md_engine as EN
import md_events as E
import md_state as S
import md_store as ST
from md_helpers import load_frames

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BOT, "tools"))
import md_run  # noqa: E402

MD_MODULES = ["md_events", "md_wire", "md_book", "md_state", "md_store", "md_engine", "md_rest",
              "md_transport", "md_feed", "md_broker"]

#: Everything an md_ module may import. No client, no signer, no broker, no store
#: of the trading state, no persistence, no config (and so no environment).
ALLOWED_IMPORTS = {
    "__future__", "base64", "collections", "dataclasses", "datetime", "fcntl", "hashlib", "json", "math",
    "os", "random", "re", "socket", "sqlite3", "ssl", "struct", "threading", "time", "typing", "urllib",
    "zlib", "ws_transport", "market_snapshot",
    "md_events", "md_wire", "md_book", "md_state", "md_store", "md_engine", "md_rest",
    "md_transport", "md_feed", "md_broker"}


def _tree(mod):
    return ast.parse(open(os.path.join(BOT, mod + ".py"), encoding="utf-8").read())


def _imports(tree):
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            out.add((n.module or "").split(".")[0])
    return out


@pytest.mark.parametrize("mod", MD_MODULES)
def test_md_modules_import_nothing_that_can_hold_a_key_or_place_an_order(mod):
    extra = _imports(_tree(mod)) - ALLOWED_IMPORTS
    assert extra == set(), f"{mod} imports {sorted(extra)}"


def _code_text(tree):
    """Identifiers and string literals of the CODE (docstrings excluded: they may
    say 'no key, no signature'; the code may not use one)."""
    doc_ids = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(n, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
                doc_ids.add(id(body[0].value))
    toks = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Name):
            toks.append(n.id)
        elif isinstance(n, ast.Attribute):
            toks.append(n.attr)
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in doc_ids:
            toks.append(n.value)
        elif isinstance(n, ast.arg):
            toks.append(n.arg)
    return toks


@pytest.mark.parametrize("mod", MD_MODULES)
def test_md_modules_read_no_environment_and_name_no_credentials(mod):
    toks = _code_text(_tree(mod))
    for ident in ("environ", "getenv", "putenv", "api_key", "api_secret", "API_KEY", "place_order",
                  "signature", "sign"):
        assert ident not in toks, f"{mod} uses the identifier {ident!r}"
    for needle in ("X-BAPI", "/v5/order", "/v5/position", "/v5/account", "/v5/asset"):
        assert not any(needle in t for t in toks), f"{mod} names {needle!r}"


def test_the_only_http_verb_is_get_and_every_endpoint_is_public_market_data():
    src = open(os.path.join(BOT, "md_rest.py"), encoding="utf-8").read()
    assert 'method="GET"' in src and "POST" not in src and "PUT" not in src and "DELETE" not in src
    import md_rest
    assert all(e.startswith("/v5/market/") for e in md_rest.ALLOWED_ENDPOINTS)
    for ep, params, _ in md_rest.bootstrap_plan("BTCUSDT"):
        assert ep in md_rest.ALLOWED_ENDPOINTS


def test_the_feed_sends_only_subscribe_unsubscribe_and_ping_frames():
    src = open(os.path.join(BOT, "md_feed.py"), encoding="utf-8").read()
    ops = set(re.findall(r'"op":\s*"(\w+)"', src))
    assert ops == {"subscribe", "unsubscribe", "ping"}


def test_the_trading_database_and_source_trees_are_not_writable_by_the_store():
    for bad in ("state/trading_state.db", "state", "artifacts", "data", "research", ""):
        with pytest.raises(ST.StoreError):
            ST.MarketStore(os.path.join(BOT, bad))


# -- the operator CLI ---------------------------------------------------------------------------------------

def build_store(path):
    store = ST.MarketStore(str(path), commit_every=1, environment="mainnet")
    eng = EN.Engine(S.MarketState("BTCUSDT"), store)
    frames = load_frames()
    t0 = frames[0]["recv_ms"]
    for c in (E.LINEAR, E.SPOT):
        eng.record(ST.CONN, wall_ms=t0, mono_s=0.0, category=c, conn_id="c-" + c,
                   payload=json.dumps({"event": "connected"}))
    for i, fr in enumerate(frames[:300]):
        mono = (fr["recv_ms"] - t0) / 1000.0
        eng.record(ST.FRAME, wall_ms=fr["recv_ms"], mono_s=mono, category=fr["category"],
                   conn_id="c-" + fr["category"], payload=fr["raw"])
        if i % 50 == 0:
            eng.log_view(mono, fr["recv_ms"])
    store.close()


def run_cli(*argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = md_run.main(list(argv))
    return code, json.loads(buf.getvalue().strip().splitlines()[-1])


def test_cli_replay_reports_determinism_and_exits_zero(tmp_path):
    build_store(tmp_path / "s")
    code, out = run_cli("replay", "--dir", str(tmp_path / "s"))
    assert code == 0 and out["deterministic"] and out["views_checked"] >= 5
    assert out["records"] > 300 and out["rejections"] == {} and out["chain_problems"] == []


def test_cli_replay_exits_nonzero_when_the_log_was_tampered_with(tmp_path):
    import sqlite3
    build_store(tmp_path / "s")
    seg = os.path.join(str(tmp_path / "s"), "md-20261104.db")
    conn = sqlite3.connect(seg)
    conn.execute("DROP TRIGGER log_no_update")
    conn.execute("UPDATE log SET wall_ms = wall_ms + 1 WHERE id = 100")
    conn.commit()
    conn.close()
    code, out = run_cli("replay", "--dir", str(tmp_path / "s"))
    assert code == 2 and not out["deterministic"] and out["chain_problems"]


def test_cli_status_summarises_the_log(tmp_path):
    build_store(tmp_path / "s")
    code, out = run_cli("status", "--dir", str(tmp_path / "s"))
    assert code == 0 and out["records_by_kind"]["frame"] == 300 and out["last_logged_view"]["environment"] == "mainnet"


def test_cli_collect_refuses_a_directory_inside_the_source_tree():
    with pytest.raises(ST.StoreError):
        md_run.main(["collect", "--dir", os.path.join(BOT, "state", "md"), "--seconds", "0"])


# -- the economics layers are pure -------------------------------------------------------------------------------

PURE = {"md_economics": {"__future__", "dataclasses", "math", "typing", "carry_costs", "strategy_evidence"},
        "candidate_carry_depth_v1": {"__future__", "hashlib", "json", "math", "dataclasses", "typing",
                                     "carry_costs", "md_economics", "md_state", "strategy_evidence"}}


@pytest.mark.parametrize("mod", sorted(PURE))
def test_the_economics_modules_are_pure(mod):
    tree = _tree(mod)
    assert _imports(tree) <= PURE[mod], sorted(_imports(tree) - PURE[mod])
    toks = _code_text(tree)
    for ident in ("environ", "getenv", "open", "socket", "time", "sleep", "place_order", "requests"):
        assert ident not in toks, f"{mod} uses {ident!r}"
