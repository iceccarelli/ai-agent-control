"""ORDER-PATH INVENTORY PROOF.

Walks the AST of every non-test source file (bot/*.py, bot/tools/*.py) and pins the COMPLETE set of places that can create an order, as ``file:qualified.function``:

  * a call to ``place_order`` / ``place_market`` / ``place_post_only`` / ``place_stop_order`` / ``place_take_profit`` / ``_submit`` (any receiver);
  * the literal ``/v5/order/create`` (or a module-level name bound to it) used anywhere but a docstring.

A NEW opening path therefore fails this test until a reviewer adds it to ``EXPECTED_SITES`` on purpose. Second, every site that can really send reaches ``BybitClient._request`` (the one transport
where the entry authority sits): no order-capable module builds its own HTTP (urllib / requests / http.client / httpx / aiohttp / socket), and the HTTP-capable modules are pinned too.

The pin is of what exists, not of what is desirable: see ``NOT_THROUGH_REQUEST`` for the sites that do not reach the real transport (all simulators or doubles) and why.
"""
from __future__ import annotations

import ast
import glob
import os
from typing import Dict, List, Set, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)

CREATE_PATH = "/v5/order/create"
ORDER_CALLS = frozenset({"place_order", "place_market", "place_post_only", "place_stop_order", "place_take_profit", "_submit"})
#: modules that can open a network connection. ``urllib.parse`` and ``http.server`` do not (string handling / a local listener), so they are not listed.
HTTP_MODULES = frozenset({"urllib.request", "urllib.error", "requests", "http.client", "httpx", "aiohttp", "socket", "websocket", "websockets", "urllib3"})


def _sources() -> List[str]:
    files = sorted(glob.glob(os.path.join(BOT, "*.py")) + glob.glob(os.path.join(BOT, "tools", "*.py")))
    return [f for f in files if os.path.basename(f) != "conftest.py"]


def _rel(path: str) -> str:
    return os.path.relpath(path, BOT).replace(os.sep, "/")


def _is_docstring(node: ast.AST, parents: Dict[ast.AST, ast.AST]) -> bool:
    p = parents.get(node)
    return isinstance(p, ast.Expr) and isinstance(node, ast.Constant) and isinstance(node.value, str)


def _is_http(mod: str) -> bool:
    return any(mod == m or mod.startswith(m + ".") for m in HTTP_MODULES)


def _scan(path: str) -> Tuple[Set[str], Set[str]]:
    """(sites, http_imports) for one file."""
    tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
    parents = {c: p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}
    # module-level names bound to the create path (``CREATE = "/v5/order/create"`` or ``CREATE, CANCEL = "/v5/order/create", ...``)
    aliases: Set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                pairs = []
                if isinstance(tgt, ast.Name):
                    pairs = [(tgt, node.value)]
                elif isinstance(tgt, (ast.Tuple, ast.List)) and isinstance(node.value, (ast.Tuple, ast.List)) and len(tgt.elts) == len(node.value.elts):
                    pairs = list(zip(tgt.elts, node.value.elts))
                for t, v in pairs:
                    if isinstance(t, ast.Name) and isinstance(v, ast.Constant) and v.value == CREATE_PATH:
                        aliases.add(t.id)

    def scope(node: ast.AST) -> str:
        names: List[str] = []
        cur = parents.get(node)
        while cur is not None:
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.append(cur.name)
            cur = parents.get(cur)
        return ".".join(reversed(names)) or "<module>"

    sites: Set[str] = set()
    http: Set[str] = set()
    rel = _rel(path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else fn.id if isinstance(fn, ast.Name) else None
            if name in ORDER_CALLS:
                sites.add(f"{rel}:{scope(node)}")
        elif isinstance(node, ast.Constant) and node.value == CREATE_PATH and not _is_docstring(node, parents):
            sites.add(f"{rel}:{scope(node)}")
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in aliases:
            sites.add(f"{rel}:{scope(node)}")
        elif isinstance(node, ast.Import):
            http.update(f"{a.name}@{scope(node)}" for a in node.names if _is_http(a.name))
        elif isinstance(node, ast.ImportFrom) and node.module:
            if _is_http(node.module):
                http.add(f"{node.module}@{scope(node)}")
            elif node.module == "urllib" and any(a.name in ("request", "error") for a in node.names):
                http.add(f"urllib.request@{scope(node)}")
    return sites, http


def _inventory() -> Tuple[Set[str], Dict[str, Set[str]]]:
    sites: Set[str] = set()
    http: Dict[str, Set[str]] = {}
    for f in _sources():
        s, h = _scan(f)
        sites |= s
        if h:
            http[_rel(f)] = h
    return sites, http


# ---- the pinned inventory ---------------------------------------------------------------------------------------------------------------------------------
#: site -> role. Roles: TRANSPORT (BybitClient: authority, count, the create request, its public entry points); CLIENT_CALLER (calls BybitClient place_*); BROKER_SEND (CarryBroker builds the
#: create body and sends it with ``self.client._request``); BROKER_CALLER (calls a CarryBroker-shaped ``place_*``: the real broker, the ledger wrapper, a shadow or a replay double);
#: GATEWAY (the testnet experiment's own allow-listed gateway, which calls ``client._request``); READER (reads recorded requests, sends nothing); SIMULATOR (in-memory exchange, no network).
EXPECTED_SITES: Dict[str, str] = {
    "bybit_connection.py:<module>": "TRANSPORT",                     # EXPOSURE_ENDPOINTS / ALLOWED sets name the path
    "bybit_connection.py:BybitClient._request": "TRANSPORT",         # entry authority + orders_submitted + transport.request
    "bybit_connection.py:BybitClient._submit": "TRANSPORT",          # the ONE create request
    "bybit_connection.py:BybitClient.place_order": "TRANSPORT",
    "bybit_connection.py:BybitClient.place_stop_order": "TRANSPORT",
    "bybit_connection.py:BybitClient.place_take_profit": "TRANSPORT",
    "carry_broker.py:CarryBroker._place_market_core": "BROKER_SEND",
    "carry_broker.py:CarryBroker._place_post_only_core": "BROKER_SEND",
    "carry_engine.py:CarryEngine._rest": "BROKER_CALLER",
    "carry_engine.py:CarryEngine._take": "BROKER_CALLER",
    "close_executor.py:CloseExecutor._send": "BROKER_CALLER",
    "ledger.py:LedgerBroker.place_market": "BROKER_CALLER",
    "ledger.py:LedgerBroker.place_post_only": "BROKER_CALLER",
    "backtest.py:SimulatedExchange.request": "SIMULATOR",
    "tools/close_testnet_experiment.py:<module>": "GATEWAY",
    "tools/close_testnet_experiment.py:Gateway.request": "GATEWAY",
    "tools/close_testnet_experiment.py:Lab.place": "GATEWAY",
    "tools/close_testnet_experiment.py:final_step": "GATEWAY",
    "tools/close_testnet_short.py:close_short": "BROKER_CALLER",
    "tools/evidence_reconcile.py:VenueIndex._add": "READER",
    "tools/evidence_reconcile.py:reconcile_carry_cycle": "READER",
    "tools/recon_packet.py:_extract_rest_order_identities": "READER",
    "tools/linear_candidate_replay.py:CandidateReplay._enter": "SIMULATOR",
    "tools/linear_candidate_replay.py:CandidateReplay._time_exit": "SIMULATOR",
    "tools/linear_stop_venue_drill.py:_abort_flatten": "CLIENT_CALLER",
    "tools/linear_stop_venue_drill.py:run_drill.attach": "CLIENT_CALLER",
    "tools/linear_stop_venue_drill.py:run_drill.flatten": "CLIENT_CALLER",
    "tools/linear_stop_venue_drill.py:run_drill.open_position": "CLIENT_CALLER",
    "tools/linear_stop_venue_drill.py:run_flatten.flatten": "CLIENT_CALLER",
    "tools/linear_stop_venue_drill.py:run_hold.attach": "CLIENT_CALLER",
    "tools/linear_stop_venue_drill.py:run_hold.open_position": "CLIENT_CALLER",
    "tools/linear_stop_venue_drill.py:run_induce_naked.attach": "CLIENT_CALLER",
    "tools/linear_stop_venue_drill.py:run_induce_naked.open_position": "CLIENT_CALLER",
    "tools/linear_stop_venue_drill.py:run_margin_doc.attach": "CLIENT_CALLER",
    "tools/linear_stop_venue_drill.py:run_margin_doc.open_position": "CLIENT_CALLER",
    "runtime_capability.py:<module>": "READER",       # reviewed: the role allow-lists NAME the order endpoint as a string; the module imports no client/transport and sends nothing
    "trading_engine.py:TradingEngine._emergency_close": "CLIENT_CALLER",
    "trading_engine.py:TradingEngine._enter": "CLIENT_CALLER",
    "trading_engine.py:TradingEngine._protect": "CLIENT_CALLER",
    "trading_engine.py:TradingEngine.close_position": "CLIENT_CALLER",
    "trading_engine.py:TradingEngine.execute": "CLIENT_CALLER",
    "trading_engine.py:TradingEngine.move_stop": "CLIENT_CALLER",
}

#: Files that can open a network connection (real transports, public fetchers, health probes, alert webhooks, sockets). The ONLY one that is also order-capable is the transport.
EXPECTED_HTTP_FILES = frozenset({
    "bybit_connection.py", "carry_shadow.py", "md_rest.py", "tools/market_collector.py", "md_transport.py", "private_ws_consumer.py", "ws_transport.py",
    "tools/append_closed_corpus.py", "tools/append_spot_corpus.py", "tools/connector_check.py", "tools/fetch_binance_klines.py", "tools/fetch_borrow_rates.py",
    "tools/fetch_funding_history.py", "tools/fetch_linear_klines_1d.py", "tools/fetch_settlement_klines.py", "tools/funding_capture.py", "tools/kill_switch_drill.py",
    "tools/mainnet_public.py", "tools/runner_alerts.py", "tools/unattended_drill.py", "tools/universe_capture.py"})
ORDER_CAPABLE_HTTP_FILES = frozenset({"bybit_connection.py"})

#: Sites that do NOT reach BybitClient._request, and why that is not an opening path to a venue.
NOT_THROUGH_REQUEST = {
    "tools/linear_candidate_replay.py": "calls an in-memory backtest.LinearSimulatedExchange.request directly; it imports no HTTP module and no BybitClient",
    "backtest.py": "an in-memory exchange; when used with a BybitClient it is injected as the TRANSPORT beneath _request, so _request (and the authority) still run first",
}
SENDER_ROLES = ("TRANSPORT", "BROKER_SEND", "GATEWAY", "CLIENT_CALLER", "BROKER_CALLER")


def _tree(rel: str) -> ast.Module:
    return ast.parse(open(os.path.join(BOT, rel), encoding="utf-8").read())


def _func(tree: ast.Module, qual: str) -> ast.AST:
    node: ast.AST = tree
    for part in qual.split("."):
        node = next(n for n in ast.walk(node) if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name == part)
    return node


def _calls(node: ast.AST, attr: str):
    for n in ast.walk(node):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == attr:
            yield n


def _src(node: ast.AST) -> str:
    return ast.unparse(node)


def test_the_set_of_order_create_sites_is_exactly_the_pinned_allow_list():
    sites, _ = _inventory()
    new, gone = sorted(sites - set(EXPECTED_SITES)), sorted(set(EXPECTED_SITES) - sites)
    assert not new, f"NEW order-capable site(s) {new}: review them, then add to EXPECTED_SITES with a role"
    assert not gone, f"pinned site(s) {gone} no longer exist: remove them from EXPECTED_SITES on purpose"
    assert set(EXPECTED_SITES.values()) <= {"TRANSPORT", "BROKER_SEND", "BROKER_CALLER", "CLIENT_CALLER", "GATEWAY", "READER", "SIMULATOR"}


def test_http_capable_modules_are_pinned_and_only_the_transport_is_also_order_capable():
    sites, http = _inventory()
    assert set(http) == EXPECTED_HTTP_FILES, (sorted(set(http) - EXPECTED_HTTP_FILES), sorted(EXPECTED_HTTP_FILES - set(http)))
    order_files = {s.split(":")[0] for s in sites}
    assert order_files & set(http) == ORDER_CAPABLE_HTTP_FILES
    # inside the transport module the connection is made in one place: RequestsTransport (urllib.request/requests nowhere else in it)
    assert http["bybit_connection.py"] == {"requests@RequestsTransport.__init__"}


def test_no_order_capable_module_builds_its_own_transport():
    sites, http = _inventory()
    for rel in {s.split(":")[0] for s in sites} - set(ORDER_CAPABLE_HTTP_FILES):
        assert rel not in http, f"{rel} can send orders AND opens its own connection"


def test_every_sender_reaches_bybit_client_request():
    # 1. BybitClient._submit sends the create request through BybitClient._request, and _request alone calls the transport after the authority
    bc = _tree("bybit_connection.py")
    submit_calls = [_src(c) for c in _calls(_func(bc, "BybitClient._submit"), "_request")]
    assert any("'POST'" in c and CREATE_PATH in c for c in submit_calls), submit_calls
    req = _func(bc, "BybitClient._request")
    assert list(_calls(req, "_authorize_new_exposure")) and list(_calls(req, "request")), "_request must run the entry authority and then the transport"
    assert not [n for n in ast.walk(bc) if isinstance(n, ast.FunctionDef) and n.name != "_request" and any(
        CREATE_PATH in _src(c) and "transport" in _src(c) for c in _calls(n, "request"))]
    # 2. the public BybitClient entry points go through _submit / place_order
    assert list(_calls(_func(bc, "BybitClient.place_order"), "_submit"))
    assert list(_calls(_func(bc, "BybitClient.place_stop_order"), "_submit"))
    assert list(_calls(_func(bc, "BybitClient.place_take_profit"), "place_order"))
    # 3. CarryBroker's two create sites send with self.client._request
    cb = _tree("carry_broker.py")
    for fn in ("CarryBroker._place_market_core", "CarryBroker._place_post_only_core"):
        sends = [_src(c) for c in _calls(_func(cb, fn), "_request") if CREATE_PATH in _src(c)]
        assert sends and all(s.startswith("self.client._request(") for s in sends), (fn, sends)
    # 4. the experiment gateway has no transport of its own: it calls client._request
    gw = _func(_tree("tools/close_testnet_experiment.py"), "Gateway.request")
    assert any(_src(c).startswith("self.client._request(") for c in _calls(gw, "_request"))
    # 5. no other file hands the create path to a request/_request call
    senders = set()
    for f in _sources():
        t = ast.parse(open(f, encoding="utf-8").read())
        for n in ast.walk(t):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in ("request", "_request") and CREATE_PATH in _src(n):
                senders.add(_rel(f))
    assert senders == {"bybit_connection.py", "carry_broker.py"}, senders


def test_sites_that_do_not_reach_request_are_simulators_with_no_network():
    sites, http = _inventory()
    for rel, why in NOT_THROUGH_REQUEST.items():
        assert rel not in http, (rel, why)
    replay = _tree("tools/linear_candidate_replay.py")
    imported = {n.module for n in ast.walk(replay) if isinstance(n, ast.ImportFrom)} | {a.name for n in ast.walk(replay) if isinstance(n, ast.Import) for a in n.names}
    assert "bybit_connection" not in imported
    assert "bt.LinearSimulatedExchange" in _src(replay)


def test_batch_and_amend_order_endpoints_are_denied_and_appear_nowhere_else():
    hits = set()
    for f in _sources():
        t = open(f, encoding="utf-8").read()
        if any(x in t for x in ("/v5/order/create-batch", "/v5/order/amend", "/v5/order/amend-batch")):
            hits.add(_rel(f))
    assert hits == {"bybit_connection.py"}, hits


def test_scanner_detects_a_new_opening_path(tmp_path):
    """Control: the scanner really flags a fresh site (so a green inventory is not a blind scanner)."""
    f = tmp_path / "evil.py"
    f.write_text('import requests\ndef sneaky(c):\n    c.place_order(1)\n    requests.post("x", json={"p": "/v5/order/create"})\n')
    sites, http = _scan(str(f))
    assert any(s.endswith(":sneaky") for s in sites) and http


if __name__ == "__main__":      # `python tests/test_order_path_inventory.py` prints the live inventory for review
    _s, _h = _inventory()
    print("\n".join(sorted(_s)))
    print("--- http")
    for k, v in sorted(_h.items()):
        print(k, sorted(v))
