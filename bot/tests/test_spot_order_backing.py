"""A spot Sell is an exit only when inventory backs it; reduce-only does not make a spot Buy one.

Reviewing the entry gate found two holes. `reduce_only` was trusted on spot,
where it is only a caller's flag (the venue does not enforce it): a spot BUY
carrying it skipped both the gate and the funds check. And a spot SELL was
exempt merely for being a Sell, with nothing proving the coin was held; with
spot margin on, an unbacked sell borrows, which is a short. Now:

  * a spot BUY is always an entry (asked of the gate; reduce_only is refused);
  * a spot SELL goes only if the wallet states a base balance that covers it
    (a reduce-only sell is clamped to what is held), else it is refused;
  * with the gate shut the same backing proof decides whether a sell is an exit;
  * spot stop orders obey the same rules (a Buy stop is an entry).

Linear reduce-only orders are venue-enforced and stay exits. Offline, on the
strict fake exchange.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bybit_connection as bc  # noqa: E402
from fake_bybit import FakeBybit  # noqa: E402
from persistence import StateStore  # noqa: E402
from test_bybit_connection import Cfg  # noqa: E402

SHUT = lambda: (False, "KILL_SWITCH_ENGAGED: test")   # noqa: E731
OPEN = lambda: (True, "kill switch clear")            # noqa: E731


@pytest.fixture()
def exchange():
    return FakeBybit()


@pytest.fixture()
def store(tmp_path):
    s = StateStore(str(tmp_path / "s.db"))
    yield s
    s.close()


@pytest.fixture()
def client(exchange, store):
    return bc.BybitClient(config=Cfg(), store=store, transport=exchange)


def creates(exchange):
    return [r for r in exchange.requests if r["url"].endswith("/order/create")]


def sent_qty(exchange):
    return float(json.loads(creates(exchange)[-1]["body"])["qty"])


# -- a spot BUY is always an entry -----------------------------------------------

@pytest.mark.parametrize("gate", [SHUT, OPEN, None])
def test_a_reduce_only_spot_buy_is_refused_whatever_the_gate_says(client, exchange, gate):
    client.entry_gate = gate
    r = client.place_order(symbol="BTCUSDT", side="Buy", qty=0.001,
                          reduce_only=True, purpose="close")
    assert r.ok is False and "REDUCE_ONLY" in r.reason
    assert creates(exchange) == []


def test_a_spot_buy_with_the_gate_shut_is_refused_even_if_flagged_reduce_only(client, exchange):
    client.entry_gate = SHUT
    r = client.place_order(symbol="BTCUSDT", side="Buy", qty=0.001, reduce_only=True)
    assert r.ok is False
    assert creates(exchange) == []


# -- a spot SELL needs inventory behind it ------------------------------------------

def test_a_backed_spot_sell_goes_with_the_gate_shut(client, exchange):
    exchange.balances["BTC"] = 1.0
    client.entry_gate = SHUT
    r = client.place_order(symbol="BTCUSDT", side="Sell", qty=0.001)
    assert r.ok is True, r.reason


def test_an_unbacked_spot_sell_is_refused_with_the_gate_shut(client, exchange):
    exchange.balances["BTC"] = 0.0
    client.entry_gate = SHUT
    r = client.place_order(symbol="BTCUSDT", side="Sell", qty=0.001)
    assert r.ok is False and creates(exchange) == []


def test_a_partly_backed_spot_sell_is_refused_with_the_gate_shut(client, exchange):
    exchange.balances["BTC"] = 0.0005
    client.entry_gate = SHUT
    r = client.place_order(symbol="BTCUSDT", side="Sell", qty=0.001)
    assert r.ok is False and creates(exchange) == []


def test_an_unreadable_wallet_refuses_a_spot_sell_with_the_gate_shut(client, exchange, monkeypatch):
    client.entry_gate = SHUT
    def boom(*a, **k):
        raise bc.PermanentAPIError("wallet down")
    monkeypatch.setattr(client, "get_coin_balance", boom)
    r = client.place_order(symbol="BTCUSDT", side="Sell", qty=0.001,
                           reduce_only=True, purpose="emergency_close")
    assert r.ok is False and creates(exchange) == []


def test_a_reduce_only_spot_sell_is_clamped_to_what_is_held(client, exchange):
    exchange.balances["BTC"] = 0.0004
    r = client.place_order(symbol="BTCUSDT", side="Sell", qty=0.001,
                           reduce_only=True, purpose="close")
    assert r.ok is True, r.reason
    assert sent_qty(exchange) == pytest.approx(0.0004)


def test_a_reduce_only_spot_sell_with_nothing_held_sends_nothing(client, exchange):
    exchange.balances["BTC"] = 0.0
    r = client.place_order(symbol="BTCUSDT", side="Sell", qty=0.001,
                           reduce_only=True, purpose="close")
    assert r.ok is False and creates(exchange) == []


def test_an_unbacked_non_reduce_only_sell_keeps_its_named_refusal(client, exchange):
    exchange.balances["BTC"] = 0.0
    r = client.place_order(symbol="BTCUSDT", side="Sell", qty=0.001)
    assert r.reason.startswith("SHORT_NOT_AVAILABLE_ON_SPOT")


def test_the_gate_is_not_asked_about_a_backed_sell_when_it_is_open(client, exchange):
    exchange.balances["BTC"] = 1.0
    client.entry_gate = OPEN
    assert client.place_order(symbol="BTCUSDT", side="Sell", qty=0.001).ok is True


# -- stops and take-profit on spot ---------------------------------------------------

def test_a_spot_buy_stop_is_an_entry_and_is_refused_with_the_gate_shut(client, exchange):
    client.entry_gate = SHUT
    r = client.place_stop_order(symbol="BTCUSDT", side="Buy", qty=0.001,
                                trigger_price=51_000.0)
    assert r.ok is False and "ENTRY_GATE_REFUSED" in r.reason
    assert creates(exchange) == []


def test_a_backed_spot_sell_stop_still_goes_with_the_gate_shut(client, exchange):
    exchange.balances["BTC"] = 1.0
    client.entry_gate = SHUT
    assert client.place_stop_order(symbol="BTCUSDT", side="Sell", qty=0.001,
                                   trigger_price=49_000.0).ok is True


def test_an_unbacked_spot_sell_stop_is_refused_with_the_gate_shut(client, exchange):
    exchange.balances["BTC"] = 0.0
    client.entry_gate = SHUT
    r = client.place_stop_order(symbol="BTCUSDT", side="Sell", qty=0.001,
                                trigger_price=49_000.0)
    assert r.ok is False and creates(exchange) == []


def test_a_backed_take_profit_goes_with_the_gate_shut(client, exchange):
    exchange.balances["BTC"] = 1.0
    client.entry_gate = SHUT
    assert client.place_take_profit(symbol="BTCUSDT", side="Sell", qty=0.001,
                                    limit_price=60_000.0).ok is True


# -- linear: reduce-only is enforced by the venue and stays an exit ---------------------

@pytest.fixture()
def linear(exchange, store):
    class LinearCfg(Cfg):
        CATEGORY = "linear"
    return bc.BybitClient(config=LinearCfg(), store=store, transport=exchange)


@pytest.mark.parametrize("side", ["Buy", "Sell"])
def test_a_linear_reduce_only_exit_is_never_asked(linear, exchange, side):
    asked = []
    linear.entry_gate = lambda: asked.append(1) or (False, "shut")
    r = linear.place_order(symbol="BTCUSDT", side=side, qty=0.001,
                           reduce_only=True, purpose="emergency_close")
    assert asked == [] and r.ok is True, r.reason


@pytest.mark.parametrize("side", ["Buy", "Sell"])
def test_a_linear_order_that_can_add_exposure_is_refused_with_the_gate_shut(linear, exchange, side):
    linear.entry_gate = SHUT
    r = linear.place_order(symbol="BTCUSDT", side=side, qty=0.001)
    assert r.ok is False and "ENTRY_GATE_REFUSED" in r.reason
    assert creates(exchange) == []


# -- a missing or unreadable switch --------------------------------------------------------

def test_the_production_gate_refuses_when_the_switch_row_is_missing(tmp_path):
    import main as _main
    s = StateStore(str(tmp_path / "s.db"))
    try:
        s._conn.execute("DELETE FROM kill_switch")
        s._conn.commit()
    except Exception:  # noqa: BLE001
        pytest.skip("cannot remove the row in this store")
    gate = _main.carry_entries_permitted_from(s)
    allowed, why = gate()
    assert allowed is False and "UNREADABLE" in why
    s.close()


# -- where a client may be constructed ---------------------------------------------------------

def test_the_modules_that_construct_a_bybit_client_are_pinned():
    """A new construction site is a new way around the entry gate. Adding one
    must be a reviewed decision, so the set is pinned."""
    import re
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    found = set()
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in ("tests", "__pycache__", "venv",
                                                "env", "site-packages",
                                                "node_modules")
                   and not d.startswith(".")]
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(base, name)
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
            if re.search(r"\bBybitClient\(", text) and name != "bybit_connection.py":
                found.add(os.path.relpath(path, root))
    assert found == {
        "main.py",                              # production: build_bot gates it
        "backtest.py",                          # offline transport only
        "tools/testnet_conformance_run.py", "tools/fund_demo.py",
        "tools/fee_payload_probe.py", "tools/linear_stop_venue_drill.py",
        "tools/recon_packet.py", "tools/paper_session_demo.py",
        "tools/drill.py", "tools/carry_preflight.py",
    }, found


def test_production_wiring_gates_the_client_it_constructs():
    import ast
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "main.py"), encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    assigned = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Attribute) and t.attr == "entry_gate"
                        for t in n.targets)]
    assert assigned, "build_bot must assign client.entry_gate"
