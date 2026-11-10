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
import os

import pytest
from typing import Dict, List, Set, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)

CREATE_PATH = "/v5/order/create"
ORDER_CALLS = frozenset({"place_order", "place_market", "place_post_only", "place_stop_order", "place_take_profit", "_submit"})
#: modules that can open a network connection. ``urllib.parse`` and ``http.server`` do not (string handling / a local listener), so they are not listed.
HTTP_MODULES = frozenset({"urllib.request", "urllib.error", "requests", "http.client", "httpx", "aiohttp", "socket", "websocket", "websockets", "urllib3"})


SKIP_DIRS = frozenset({"tests", "state", "__pycache__", ".git", ".venv"})
#: names that mark an order-creating endpoint (as a substring of any string constant / folded string). ``/v5/order/cancel`` and the read endpoints are deliberately not listed;
#: the bare prefix IS listed because a path built as ``"/v5/order/" + "create"`` must not escape.
ORDER_ENDPOINT_MARKERS = ("/v5/order/create", "/v5/order/amend", "/v5/order/", "v5/order")
_BATCH_OK = ("/v5/order/cancel", "/v5/order/realtime", "/v5/order/history", "/v5/order/execution-list")
#: modules whose mere import is flagged: process spawning (curl, a shell, a second Python) is a way around the transport.
PROCESS_MODULES = frozenset({"subprocess", "pty", "multiprocessing", "pexpect"})
OS_PROCESS_ATTRS = frozenset({"system", "popen", "execl", "execle", "execlp", "execlpe", "execv", "execve", "execvp", "execvpe", "spawnl", "spawnle", "spawnlp", "spawnlpe",
                              "spawnv", "spawnve", "spawnvp", "spawnvpe", "posix_spawn", "posix_spawnp", "startfile", "fork", "forkpty", "Popen", "create_subprocess_exec",
                              "create_subprocess_shell"})
WEAK_REASONS = frozenset({"dynamic-attr", "dict-access"})
ORDER_REASONS = frozenset({"order-attr", "order-name", "order-call", "endpoint", "dynamic-attr!"})
DYNAMIC_ACCESS_FUNCS = frozenset({"getattr", "setattr", "delattr", "hasattr", "vars"})
DYNAMIC_CODE_FUNCS = frozenset({"eval", "exec", "compile"})
WRAPPERS = frozenset({"partial", "partialmethod", "attrgetter", "methodcaller", "wraps"})


def _sources() -> List[str]:
    """Every .py under bot/ except tests/ (and state/, which no test may open, and caches)."""
    out: List[str] = []
    for root, dirs, files in os.walk(BOT):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        out += [os.path.join(root, f) for f in sorted(files) if f.endswith(".py") and f != "conftest.py"]
    return sorted(out)


def _rel(path: str) -> str:
    return os.path.relpath(path, BOT).replace(os.sep, "/")


def _is_docstring(node: ast.AST, parents: Dict[ast.AST, ast.AST]) -> bool:
    p = parents.get(node)
    return isinstance(p, ast.Expr) and isinstance(node, ast.Constant) and isinstance(node.value, str)


def _is_http(mod: str) -> bool:
    return any(mod == m or mod.startswith(m + ".") for m in HTTP_MODULES)


def _order_looking(text: str) -> bool:
    """An attribute name that is, or builds, one of the order-call names (``place_*``, ``_submit`` exactly). ``orders_submitted`` (a counter) is not one."""
    low = text.lower().strip()
    return low == "_submit" or "place_" in low or any(c in low for c in ORDER_CALLS if c != "_submit")


def _endpoint_hit(text: str) -> bool:
    text = text.replace("/docs/v5/order", "/docs/v5-order")        # a documentation URL (bybit-exchange.github.io/docs/v5/order/execution) is prose, not an endpoint
    if not any(m in text for m in ORDER_ENDPOINT_MARKERS):
        return False
    return not any(text.startswith(ok) for ok in _BATCH_OK)


def _fold(node: ast.AST, env: Dict[str, str]):
    """Resolve a string built from constants: literal, ``+``, ``%``/``.format`` of constants, f-string of constants, ``''.join([..])``, a module-level constant name. Else None."""
    if isinstance(node, ast.Constant):
        if isinstance(node.value, str):
            return node.value
        if isinstance(node.value, bytes):
            return node.value.decode("latin-1")
        return None
    if isinstance(node, ast.Name):
        return env.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        l, r = _fold(node.left, env), _fold(node.right, env)
        if isinstance(node.op, ast.Add):
            return l + r if l is not None and r is not None else None
        return l if l is not None and "%" not in l else None
    if isinstance(node, ast.JoinedStr):
        parts = [_fold(v, env) if not isinstance(v, ast.FormattedValue) else _fold(v.value, env) for v in node.values]
        return "".join(parts) if all(p is not None for p in parts) else None
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "join" and len(node.args) == 1:
        sep = _fold(node.func.value, env)
        if sep is not None and isinstance(node.args[0], (ast.List, ast.Tuple)):
            parts = [_fold(e, env) for e in node.args[0].elts]
            return sep.join(parts) if all(p is not None for p in parts) else None
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format":
        base = _fold(node.func.value, env)
        return base if base is not None and "{" not in base else None
    return None


def _scan_detail(path: str) -> Tuple[Dict[str, Set[str]], Set[str]]:
    """({site: {reason, ...}}, http_imports) for one file. ``site`` is ``file:qualified.scope``."""
    tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
    parents = {c: p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}
    env: Dict[str, str] = {}                       # module-level names bound to a foldable string
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                pairs = []
                if isinstance(tgt, ast.Name):
                    pairs = [(tgt, node.value)]
                elif isinstance(tgt, (ast.Tuple, ast.List)) and isinstance(node.value, (ast.Tuple, ast.List)) and len(tgt.elts) == len(node.value.elts):
                    pairs = list(zip(tgt.elts, node.value.elts))
                for t, v in pairs:
                    folded = _fold(v, env)
                    if isinstance(t, ast.Name) and folded is not None:
                        env[t.id] = folded
    aliases = {k for k, v in env.items() if _endpoint_hit(v)}

    def scope(node: ast.AST) -> str:
        names: List[str] = []
        cur = parents.get(node)
        while cur is not None:
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.append(cur.name)
            cur = parents.get(cur)
        return ".".join(reversed(names)) or "<module>"

    sites: Dict[str, Set[str]] = {}
    http: Set[str] = set()
    rel = _rel(path)

    weak: Dict[str, Set[str]] = {}       # generic reflection with a non-literal name: only counts in a file that is already order-capable (else it is dataclass/config plumbing)

    def hit(node: ast.AST, reason: str) -> None:
        if reason in WEAK_REASONS:
            weak.setdefault(f"{rel}:{scope(node)}", set()).add(reason)
        else:
            sites.setdefault(f"{rel}:{scope(node)}", set()).add(reason)

    def dyn_name_arg(arg: ast.AST) -> bool:
        """True when a name argument is non-literal (cannot be proven benign) or resolves to an order-looking name."""
        folded = _fold(arg, env)
        return folded is None or _order_looking(folded)

    def import_target(mod: str, node: ast.AST) -> None:
        if _is_http(mod):
            http.add(f"{mod}@{scope(node)}")
        if mod.split(".")[0] in PROCESS_MODULES or mod == "os.system":
            hit(node, "process")

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            if node.attr in ORDER_CALLS:                                   # a read, a call, a store: any mention of the order-call name
                hit(node, "order-attr")
            if node.attr in OS_PROCESS_ATTRS:
                hit(node, "process")
            if node.attr == "__dict__" and not isinstance(parents.get(node), (ast.Call, ast.Attribute, ast.Subscript)):
                hit(node, "dict-access")                                   # ``d = c.__dict__`` hands out every attribute by name
            if node.attr == "__dict__":
                par = parents.get(node)
                if isinstance(par, ast.Subscript) and par.value is node and dyn_name_arg(par.slice):
                    hit(node, "dict-access")
                if isinstance(par, ast.Attribute):
                    gp = parents.get(par)
                    if isinstance(gp, ast.Call) and gp.func is par and gp.args and dyn_name_arg(gp.args[0]):
                        hit(node, "dict-access")
        elif isinstance(node, ast.Name):
            if isinstance(node.ctx, ast.Load) and node.id in ORDER_CALLS:
                hit(node, "order-name")
            elif isinstance(node.ctx, ast.Load) and node.id in aliases:
                hit(node, "endpoint")
            elif isinstance(node.ctx, ast.Load) and node.id in DYNAMIC_ACCESS_FUNCS | DYNAMIC_CODE_FUNCS | {"__import__"}:
                par = parents.get(node)
                if not (isinstance(par, ast.Call) and par.func is node):   # the function passed around as a value (map(getattr, ..), alias = getattr)
                    hit(node, "dynamic-ref")
        elif isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else fn.id if isinstance(fn, ast.Name) else None
            if name in ORDER_CALLS:
                hit(node, "order-call")
            if name in DYNAMIC_ACCESS_FUNCS and isinstance(fn, ast.Name):
                if name == "vars":
                    hit(node, "dict-access")
                elif len(node.args) >= 2 and _fold(node.args[1], env) is not None and _order_looking(_fold(node.args[1], env)):
                    hit(node, "dynamic-attr!")                          # a literal / folded order-looking name: always flagged
                elif len(node.args) >= 2 and dyn_name_arg(node.args[1]):
                    par = parents.get(node)
                    if isinstance(par, ast.Call) and par.func is node:
                        hit(node, "dynamic-attr!")                      # getattr(x, non_literal)(...): the result is called
                    elif name in ("setattr", "delattr"):
                        hit(node, "dynamic-attr!")                      # a non-literal setattr/delattr can swap or hide a method
                    else:
                        hit(node, "dynamic-attr")
                elif len(node.args) < 2:
                    hit(node, "dynamic-attr")
            if name in DYNAMIC_CODE_FUNCS and isinstance(fn, ast.Name):
                hit(node, "dynamic-code")
            if name in ("join", "format"):
                folded_call = _fold(node, env)
                if folded_call is not None and _endpoint_hit(folded_call):
                    hit(node, "endpoint")
            if name in WRAPPERS:
                for a in list(node.args) + [k.value for k in node.keywords]:
                    folded = _fold(a, env)
                    if (folded is not None and _order_looking(folded)) or (isinstance(a, ast.Name) and a.id in DYNAMIC_ACCESS_FUNCS):
                        hit(node, "wrapper")
            if name in ("import_module", "__import__") or (isinstance(fn, ast.Attribute) and fn.attr == "__import__"):
                arg0 = node.args[0] if node.args else None
                folded = _fold(arg0, env) if arg0 is not None else None
                if folded is None:
                    hit(node, "dynamic-import")
                else:
                    import_target(folded, node)
            if name == "find_spec" or name == "spec_from_file_location" or name == "load_module":
                hit(node, "dynamic-import")
            if name in OS_PROCESS_ATTRS and isinstance(fn, ast.Name):
                hit(node, "process")
        elif isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)) and not _is_docstring(node, parents):
            text = node.value if isinstance(node.value, str) else node.value.decode("latin-1")
            if _endpoint_hit(text):
                hit(node, "endpoint")
        elif isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
            folded = _fold(node, env)
            if folded is not None and _endpoint_hit(folded):
                hit(node, "endpoint")
        elif isinstance(node, ast.JoinedStr):
            folded = _fold(node, env)
            if folded is not None and _endpoint_hit(folded):
                hit(node, "endpoint")
        elif isinstance(node, ast.Import):
            for a in node.names:
                import_target(a.name, node)
        elif isinstance(node, ast.ImportFrom) and node.module:
            import_target(node.module, node)
            if node.module == "urllib" and any(a.name in ("request", "error") for a in node.names):
                http.add(f"urllib.request@{scope(node)}")
            if node.module == "os" and any(a.name in OS_PROCESS_ATTRS for a in node.names):
                hit(node, "process")
            if node.module == "importlib" or any(a.name in ORDER_CALLS for a in node.names):
                if any(a.name in ORDER_CALLS for a in node.names):
                    hit(node, "order-name")
            if node.module in ("importlib", "importlib.util") and any(a.name in ("import_module", "__import__") for a in node.names):
                pass                                                    # the call itself is judged above
    if any(r in ORDER_REASONS for rs in sites.values() for r in rs):
        for k, v in weak.items():
            sites.setdefault(k, set()).update(v)
    return sites, http


def _scan(path: str) -> Tuple[Set[str], Set[str]]:
    """(sites, http_imports) for one file."""
    detail, http = _scan_detail(path)
    return set(detail), http


def _order_sites(sites: Set[str]) -> Set[str]:
    """Sites that can build or send an order (everything except the reviewed process-spawn sites)."""
    return {s for s in sites if not EXPECTED_SITES.get(s, "").startswith("PROCESS_")}


#: modules that both open a connection AND spawn processes (reviewed): a public-data reader that runs ``git``; the drill supervisor's HTTP probe that spawns pinned drill tools.
PROCESS_AND_HTTP_FILES = frozenset({"carry_shadow.py", "tools/unattended_drill.py"})


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
    # TESTNET execution adapter (reviewed): names the create path as the module constant CREATE and sends it ONLY with ``self.client._request("POST", CREATE, ..., retries=0)`` -- the
    # one transport where the signing, the TESTNET_EXECUTOR role capability, the exact-host check and the MultiSymbolAuthority sit. It builds no HTTP of its own (pinned below).
    "tools/ms_testnet_executor.py:<module>": "GATEWAY",
    "tools/ms_testnet_executor.py:TestnetExecutor.send_order": "GATEWAY",
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
    "backtest.py:walk_forward": "SIMULATOR",         # reviewed: ``BacktestConfig(**cfg.__dict__)`` copies a config in the simulator module; no order name involved
    "bybit_connection.py:BybitClient.get_instrument_filters": "TRANSPORT",    # reviewed: ``getattr(filters, name)`` over fixed instrument-filter field names (reads)
    "carry_engine.py:CarryEngine._tick_book": "BROKER_CALLER",    # reviewed: ``getattr(view, "perp_book" if .. else "spot_book")`` reads a snapshot's book; no order name
    "ledger.py:LedgerBroker._read_touch": "BROKER_CALLER",        # reviewed: getattr(self.inner, name) over the two literal READ names get_book_depth / get_book_top
    "runtime_capability.py:CapabilityTransport.__getattr__": "READER",   # reviewed: the guard proxy delegates unknown attributes to the inner transport; ``request`` is defined on the proxy and is the guarded path
    "tools/close_testnet_experiment.py:execute_run": "GATEWAY",   # reviewed: ``getattr(lab, spec["fn"])()`` over the static EXPERIMENTS table (fn names e1..e10); every order goes through Lab.place / Gateway.request
    "tools/close_testnet_experiment.py:compute_sizing": "GATEWAY",       # reviewed: getattr(inst, name) over instrument-filter field names
    "tools/close_testnet_experiment.py:static_refusal": "GATEWAY",       # reviewed: getattr(cfg, name) over the BYBIT_API_KEY / BYBIT_API_SECRET names
    # process spawning, reviewed one by one: none of these builds an order request; children that can trade are the pinned tools above and run their own entry authority
    "carry_shadow.py:<module>": "PROCESS_GIT",                    # imports subprocess; runs ``git rev-parse`` / ``git status`` for the build id
    "tools/carry_core.py:<module>": "PROCESS_GIT",                # ``make -C cpp`` to build the native core
    "tools/carry_oos_audit.py:<module>": "PROCESS_GIT",           # ``git -C repo ...`` provenance
    "tools/data_freshness_check.py:<module>": "PROCESS_UNUSED",   # imports subprocess, no spawn call in the file
    "tools/fetch_real_data.py:<module>": "PROCESS_GIT",           # ``git clone --depth 1`` of the public corpus repo
    "tools/oos_stability_q1q2.py:<module>": "PROCESS_UNUSED",     # imports subprocess, no spawn call in the file
    "tools/provenance.py:<module>": "PROCESS_GIT",                # git head / status
    "tools/runner_supervisor.py:<module>": "PROCESS_SUPERVISOR",  # supervises monitor/recorder command lines given by the operator
    "tools/runner_supervisor.py:Supervisor.__init__": "PROCESS_SUPERVISOR",
    "tools/runner_supervisor.py:Supervisor._spawn": "PROCESS_SUPERVISOR",
    "tools/runner_supervisor.py:Supervisor._terminate": "PROCESS_SUPERVISOR",
    "tools/runner_supervisor.py:main": "PROCESS_SUPERVISOR",
    "tools/secret_scan.py:<module>": "PROCESS_GIT",               # ``git ls-files``
    "tools/shadow_replay.py:<module>": "PROCESS_UNUSED",          # imports subprocess, no spawn call in the file
    "tools/train_policy.py:<module>": "PROCESS_GIT",              # git rev for the training fingerprint
    "tools/unattended_drill.py:<module>": "PROCESS_SUPERVISOR",   # runs the testnet drill tools (incl. close_testnet_short --arm) as children; each child is a pinned tool under its own authority
    "tools/unattended_drill.py:run_child": "PROCESS_SUPERVISOR",
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
    "tools/mainnet_public.py", "tools/runner_alerts.py", "tools/unattended_drill.py", "tools/universe_capture.py",
    # the unattended-Fly additions: Supabase evidence REST (https *.supabase.co only), the heartbeat monitor reading it, and the notifier webhook; none is order-capable (asserted below)
    "tools/exec_evidence.py", "tools/health_monitor.py", "tools/notifier.py"})
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
    assert set(EXPECTED_SITES.values()) <= {"TRANSPORT", "BROKER_SEND", "BROKER_CALLER", "CLIENT_CALLER", "GATEWAY", "READER", "SIMULATOR", "PROCESS_GIT", "PROCESS_UNUSED", "PROCESS_SUPERVISOR"}


def test_http_capable_modules_are_pinned_and_only_the_transport_is_also_order_capable():
    sites, http = _inventory()
    assert set(http) == EXPECTED_HTTP_FILES, (sorted(set(http) - EXPECTED_HTTP_FILES), sorted(EXPECTED_HTTP_FILES - set(http)))
    order_files = {s.split(":")[0] for s in _order_sites(sites)}
    assert order_files & set(http) == ORDER_CAPABLE_HTTP_FILES
    assert {s.split(":")[0] for s in sites} & set(http) == ORDER_CAPABLE_HTTP_FILES | PROCESS_AND_HTTP_FILES
    # inside the transport module the connection is made in one place: RequestsTransport (urllib.request/requests nowhere else in it)
    assert http["bybit_connection.py"] == {"requests@RequestsTransport.__init__"}


def test_no_order_capable_module_builds_its_own_transport():
    sites, http = _inventory()
    for rel in {s.split(":")[0] for s in _order_sites(sites)} - set(ORDER_CAPABLE_HTTP_FILES):
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


#: Positive controls for the scanner ITSELF: every blind-spot form found by the red team (plus the obvious siblings) must be CAUGHT, with the stated reason.
BLIND_SPOT_FIXTURES = {
    "alias of the bound method": ("def f(c):\n    po = c.place_order\n    po({})\n", "order-attr"),
    "bare attribute read, never called": ("def f(c):\n    return c.place_market\n", "order-attr"),
    "attribute store (monkeypatch)": ("def f(c, g):\n    c._submit = g\n", "order-attr"),
    "from-import of an order name": ("from x import place_order\n", "order-name"),
    "bare name read": ("def f(place_order):\n    return place_order\n", "order-name"),
    "getattr with a concatenated name": ("def f(c):\n    getattr(c, 'place_' + 'order')({})\n", "dynamic-attr!"),
    "getattr with an f-string name": ("def f(c):\n    getattr(c, f'{\"place\"}_order')({})\n", "dynamic-attr!"),
    "getattr with a literal order name": ("def f(c):\n    return getattr(c, 'place_post_only', None)\n", "dynamic-attr!"),
    "getattr with a non-literal name, result called": ("def f(c, n):\n    getattr(c, n)({})\n", "dynamic-attr!"),
    "setattr with a non-literal name": ("def f(c, n, g):\n    setattr(c, n, g)\n", "dynamic-attr!"),
    "getattr passed as a value": ("def f(c):\n    g = getattr\n    return g(c, 'x')\n", "dynamic-ref"),
    "functools.partial of the method": ("import functools\ndef f(c):\n    functools.partial(c.place_order, {})()\n", "order-attr"),
    "partial over a built name": ("import functools\ndef f(c):\n    return functools.partial(getattr, c, 'place_' + 'order')\n", "wrapper"),
    "operator.attrgetter of the name": ("import operator\ndef f(c):\n    return operator.attrgetter('place_order')(c)\n", "wrapper"),
    "lambda wrapping the method": ("def f(c):\n    return lambda **k: c.place_order(**k)\n", "order-attr"),
    "importlib with a concatenated module": ("import importlib\ndef f():\n    return importlib.import_module('url' + 'lib.request')\n", "http"),
    "importlib with a non-literal module": ("import importlib\ndef f(n):\n    return importlib.import_module(n)\n", "dynamic-import"),
    "__import__ of an http module": ("def f():\n    return __import__('requests').post('https://x')\n", "http"),
    "__import__ with a non-literal": ("def f(n):\n    return __import__(n)\n", "dynamic-import"),
    "subprocess curl": ("import subprocess\ndef f():\n    subprocess.run(['curl', '-X', 'POST', 'https://api.bybit.com/v5/order/' + 'create'])\n", "process"),
    "subprocess via __import__": ("def f():\n    return __import__('sub' + 'process')\n", "process"),
    "os.system": ("import os\ndef f():\n    os.system('curl x')\n", "process"),
    "os.popen": ("import os\ndef f():\n    os.popen('curl x')\n", "process"),
    "os.execv": ("import os\ndef f():\n    os.execv('/usr/bin/curl', ['curl'])\n", "process"),
    "from os import system": ("from os import system\n", "process"),
    "eval of a string": ("def f(s):\n    return eval(s)\n", "dynamic-code"),
    "path built by concatenation": ("def f(c):\n    c._request('POST', '/v5/order/' + 'create', body={})\n", "endpoint"),
    "path built with a split literal": ("def f(c):\n    c._request('POST', '/v5/or' + 'der/cre' + 'ate')\n", "endpoint"),
    "path built with join": ("def f(c):\n    c._request('POST', ''.join(['/v5/', 'order/', 'create']))\n", "endpoint"),
    "path built from a module-level piece": ("P = '/v5/order/'\ndef f(c):\n    c._request('POST', P + 'create')\n", "endpoint"),
    "path as an f-string with a variable tail": ("def f(c, t):\n    c._request('POST', f'/v5/order/{t}')\n", "endpoint"),
    "batch endpoint": ("def f(c):\n    c._request('POST', '/v5/order/create-batch')\n", "endpoint"),
    "amend endpoint": ("def f(c):\n    c._request('POST', '/v5/order/amend')\n", "endpoint"),
    "__dict__ lookup by non-literal name": ("def f(c, n):\n    return c.__dict__[n]\n", "dict-access"),
    "__dict__ handed out": ("def f(c):\n    d = c.__dict__\n    return d\n", "dict-access"),
}


@pytest.mark.parametrize("label", sorted(BLIND_SPOT_FIXTURES))
def test_scanner_catches_every_known_blind_spot_form(tmp_path, label):
    src, reason = BLIND_SPOT_FIXTURES[label]
    f = tmp_path / "evil.py"
    # the weak reflection reasons only count in an already order-capable file, so give those fixtures one
    f.write_text(src + ("\ndef _anchor(c):\n    c.place_order(1)\n" if reason == "dict-access" else ""))
    detail, http = _scan_detail(str(f))
    if reason == "http":
        assert http, (label, detail)
    else:
        assert any(reason in rs for site, rs in detail.items() if not site.endswith(":_anchor")), (label, detail)
    sites, _ = _scan(str(f))
    assert sites or http


def test_benign_code_is_not_flagged_by_the_scanner(tmp_path):
    """Negative control: the scanner is not a blanket alarm. Doc URLs, counters, urllib.parse and literal getattr are quiet."""
    f = tmp_path / "ok.py"
    f.write_text(
        '"""docstring mentioning /v5/order/create and place_order"""\n'
        'from urllib.parse import urlencode\n'
        'SRC = "https://bybit-exchange.github.io/docs/v5/order/execution"\n'
        'CANCEL = "/v5/order/cancel"\n'
        'def f(c, cfg, n):\n'
        '    getattr(cfg, "ENTRIES_ENABLED", True)\n'
        '    getattr(cfg, n, None)\n'
        '    return int(getattr(c, "orders_submitted", 0)), CANCEL, SRC, urlencode({})\n')
    assert _scan_detail(str(f)) == ({}, set())


def test_scan_covers_every_python_file_under_bot_except_tests_and_state():
    rels = {_rel(f) for f in _sources()}
    assert "main.py" in rels and "tools/secret_scan.py" in rels and "signals/funding_carry_fade_v1.py" in rels
    assert not [r for r in rels if r.startswith(("tests/", "state/"))]
    on_disk = set()
    for root, dirs, files in os.walk(BOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        on_disk |= {_rel(os.path.join(root, f)) for f in files if f.endswith(".py") and f != "conftest.py"}
    assert rels == on_disk


def test_no_non_python_script_under_bot_names_the_create_endpoint_or_shells_out_to_it():
    for root, dirs, files in os.walk(BOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for name in files:
            if name.endswith((".sh", ".bash")):
                text = open(os.path.join(root, name), encoding="utf-8", errors="replace").read()
                assert "/v5/order" not in text, name


if __name__ == "__main__":      # `python tests/test_order_path_inventory.py` prints the live inventory for review
    _s, _h = _inventory()
    print("\n".join(sorted(_s)))
    print("--- http")
    for k, v in sorted(_h.items()):
        print(k, sorted(v))


# ---- history ingestion + corpus audit (market_history, data_freshness_audit) ---------------------------------------------------------------------------------
def test_market_history_and_the_corpus_audit_add_no_order_site_and_no_connection_of_their_own():
    """Reason for this pin: ``tools/market_history.py`` pages five PUBLIC history paths (funding, kline, mark-price-kline, index-price-kline, open-interest) and
    ``tools/data_freshness_audit.py`` only reads files. Neither may open a connection (the one primitive is ``market_collector.http_request_to`` behind the public_collector
    role, so they are NOT new members of EXPECTED_HTTP_FILES) and neither may be an order site, so EXPECTED_SITES / EXPECTED_HTTP_FILES stay exactly as they were."""
    sites, http = _inventory()
    for rel in ("tools/market_history.py", "tools/data_freshness_audit.py"):
        assert rel not in http, f"{rel} opens its own connection"
        assert not {s for s in sites if s.startswith(rel + ":")}, f"{rel} became an order-capable or process-spawning site"
    assert "tools/market_collector.py" in EXPECTED_HTTP_FILES                      # the single transport the history tool reuses
    assert "tools/market_history.py" not in EXPECTED_HTTP_FILES and "tools/data_freshness_audit.py" not in EXPECTED_HTTP_FILES


# ---- testnet execution path (ms_testnet_transport / ms_testnet_executor / ms_testnet_session) ---------------------------------------------------------------------
def test_testnet_execution_modules_open_no_connection_of_their_own_and_send_only_through_the_client_request():
    """Reason for these pins: the testnet executor is the one NEW order-create site. (1) It is pinned above as GATEWAY, not as a transport: none of the three new modules imports an
    HTTP library (the real connection is ``bybit_connection.RequestsTransport``, subclassed without redirects / proxies in ``ms_testnet_transport``), so EXPECTED_HTTP_FILES and
    ORDER_CAPABLE_HTTP_FILES do not change. (2) Its create is ``self.client._request`` and nothing else, so the entry authority and the role capability run first. (3) The session tool
    and the transport name no order endpoint at all and are not order sites. (4) The signing code is NOT re-implemented: no ``hmac`` / ``hashlib.sha256`` signing in these modules."""
    sites, http = _inventory()
    for rel in ("tools/ms_testnet_transport.py", "tools/ms_testnet_executor.py", "tools/ms_testnet_session.py"):
        assert rel not in http, f"{rel} opens its own connection"
        mod = _tree(rel)
        imported = {a.name for n in ast.walk(mod) if isinstance(n, ast.Import) for a in n.names} | {n.module for n in ast.walk(mod) if isinstance(n, ast.ImportFrom)}
        assert "hmac" not in imported, f"{rel} re-implements request signing"
        assert not [n for n in ast.walk(mod) if isinstance(n, ast.Attribute) and n.attr in ("_sign", "new")], rel       # BybitClient._sign / hmac.new are not called from here
    assert {s for s in sites if s.startswith("tools/ms_testnet_transport.py:") or s.startswith("tools/ms_testnet_session.py:")} == set()
    ex = _tree("tools/ms_testnet_executor.py")
    sends = [_src(c) for c in _calls(ex, "_request") if "CREATE" in _src(c)]
    assert sends and all(c.startswith("self.client._request('POST', CREATE") for c in sends), sends
    assert not [n for n in ast.walk(ex) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in ("request", "post", "get") and "client" not in _src(n.func)
                and "transport" in _src(n)]
    # the only inner transport the session can build is the no-redirect subclass of the pinned RequestsTransport
    tr = _tree("tools/ms_testnet_transport.py")
    bases = [_src(b) for n in ast.walk(tr) if isinstance(n, ast.ClassDef) and n.name == "NoRedirectInner" for b in n.bases]
    assert bases == ["bc.RequestsTransport"]


def test_new_modules_are_not_order_capable_http_files_and_the_authority_admits_only_the_exact_transport():
    assert "tools/ms_testnet_executor.py" not in EXPECTED_HTTP_FILES and "tools/ms_testnet_transport.py" not in EXPECTED_HTTP_FILES
    src = open(os.path.join(BOT, "tools", "ms_authority.py"), encoding="utf-8").read()
    assert "is_testnet_executor_transport" in src and "isinstance(transport, RC.CapabilityTransport) and cap is not None" not in src


def test_collector_and_monitor_roles_remain_order_incapable_with_the_new_executor_present():
    """Reason: adding an executor must not widen the other roles. They still cannot POST or sign an order path on the testnet host."""
    import runtime_capability as RC
    for role in (RC.PUBLIC_COLLECTOR, RC.READ_ONLY_MONITOR, RC.EVIDENCE_BACKUP):
        cap = RC.Capability.for_role(role, venue="testnet")
        for ep in ("/v5/order/create", "/v5/order/cancel", "/v5/position/set-leverage"):
            assert cap.check("POST", ep, True, cap.host) is not None
        assert cap.check("GET", "/v5/user/query-api", True, cap.host) in (None, "ENDPOINT_NOT_ALLOWED", "NO_VENUE_ACCESS: evidence_backup may not make any HTTP request") or role == RC.PUBLIC_COLLECTOR
