"""Round 22: `venue-snapshot` is the executor's independent, read-only, account-wide end-state read: every symbol's perp, spot balances, USDT, and ordinary AND conditional open orders. It sends nothing."""
from __future__ import annotations

import json
import os
import sys
from decimal import Decimal as D

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (HERE, os.path.dirname(HERE), os.path.join(os.path.dirname(HERE), "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import mechanics_verify as MV  # noqa: E402
import ms_testnet_autopilot as AP  # noqa: E402
from ms_testnet_world import World  # noqa: E402


@pytest.fixture
def w(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def take(w, **kw):
    lines = []
    code = AP.cmd_venue_snapshot(w.env, w.clock.now, kw.pop("symbol", "BTCUSDT"), kw.pop("out_path", None), inner=w.fake, out=lines.append)
    return code, lines


def foreign_order(w, symbol="ETHUSDT", status="New"):
    w.fake.orders["OFOREIGN1"] = {"orderId": "OFOREIGN1", "orderLinkId": "foreign-1", "symbol": symbol, "side": "Buy", "orderType": "Limit", "qty": "0.01", "price": "1000", "status": status, "cum": D(0),
                                  "avg": D(0), "reduceOnly": False, "category": "linear", "tif": "GTC", "created": w.fake.now_ms(), "qty_d": D("0.01"), "marketUnit": None}


def test_a_clean_account_snapshots_complete_flat_and_empty_and_sends_nothing(w):
    code, lines = take(w)
    snap = json.loads("\n".join(lines))
    assert code == 0 and snap["schema"] == MV.SNAPSHOT_SCHEMA and snap["complete"] is True and snap["venue"] == "testnet"
    assert all(abs(v) == 0 for v in snap["linear_positions"].values())
    assert snap["open_orders_active"] == [] and snap["open_orders_conditional"] == [] and snap["usdt_balance"] is not None
    assert MV.snapshot_problems(snap) == []
    assert not [r for r in w.fake.requests if r[0] == "POST"], "a snapshot must send no order and no other write"


def test_a_position_on_another_symbol_and_a_foreign_resting_order_are_seen_account_wide(w):
    w.fake.inject_position("ETHUSDT", -0.2)
    foreign_order(w)
    code, lines = take(w, symbol="BTCUSDT")                                          # the executor's own symbol is BTCUSDT only
    snap = json.loads("\n".join(lines))
    assert code == 0 and snap["linear_positions"].get("ETHUSDT") == -0.2
    assert [o["order_id"] for o in snap["open_orders_active"]] == ["OFOREIGN1"] and snap["open_orders_active"][0]["symbol"] == "ETHUSDT"


def test_an_incomplete_read_is_not_complete_and_is_not_read_as_no_open_orders(w):
    w.fake.inject("/v5/order/realtime", "http500", times=50)
    code, lines = take(w)
    snap = json.loads("\n".join(lines))
    assert code == 3 and snap["complete"] is False
    assert MV.snapshot_problems(snap) and "SNAPSHOT_INCOMPLETE" in MV.snapshot_problems(snap)


def test_a_malformed_wallet_answer_is_incomplete_not_a_zero_balance(w):
    w.fake.inject("/v5/account/wallet-balance", "malformed", times=50)
    code, lines = take(w)
    assert code == 3 and json.loads("\n".join(lines))["complete"] is False


def test_the_command_writes_only_the_requested_file_and_refuses_without_the_dedicated_credentials(w, tmp_path):
    out = tmp_path / "snap.json"
    code, _lines = take(w, out_path=str(out))
    assert code == 0 and json.loads(out.read_text())["complete"] is True
    lines = []
    env = {k: v for k, v in w.env.items() if "TESTNET_API" not in k}
    assert AP.cmd_venue_snapshot(env, w.clock.now, "BTCUSDT", None, inner=w.fake, out=lines.append) == 3
    assert json.loads(lines[0])["reason"] == "DEDICATED_TESTNET_CREDENTIALS_MISSING"


def test_the_snapshot_flows_into_the_verifier_and_a_foreign_conditional_order_fails_it(w):
    """End to end on the fake venue: a real session, the real snapshot command, the real verifier."""
    w.fake.funding_prints = []                      # the public funding history answers COMPLETE and empty: "no settlement inside the window" is then a statement, not an unknown
    digest = w.ready("BTCUSDT", 100.0, hold_seconds=0)
    assert w.execute(digest) == 0, w.text()
    w.clock.sleep(2)                                # a real snapshot is taken after the last fill; the simulated clock only moves when told to
    code, lines = take(w)
    snap = json.loads("\n".join(lines))
    assert code == 0
    res = MV.verify(w.state_dir, snap, require_funding=False)
    assert res["overall"] == MV.VERIFIED, [(c["id"], c["verdict"], c["detail"][:120]) for c in res["checks"] if c["verdict"] != "PASS"]
    snap["open_orders_conditional"] = [{"order_id": "SL1", "symbol": "ETHUSDT", "status": "Untriggered"}]            # a stop left on ANOTHER symbol
    bad = MV.verify(w.state_dir, snap, require_funding=False)
    assert bad["overall"] == MV.FAILED


def test_the_executors_account_wide_order_read_covers_every_filter_and_both_categories(w):
    from ms_testnet_executor import TestnetExecutor as TE_
    assert TE_.ORDER_FILTERS == ("Order", "StopOrder", "tpslOrder")
    take(w)
    reads = [r for r in w.fake.requests if r[1] == "/v5/order/realtime"]
    seen = {(q["category"], q.get("orderFilter")) for _m, _p, q, _b in reads}
    assert {(c, f) for c in ("linear", "spot") for f in TE_.ORDER_FILTERS} <= seen
    assert all(q.get("settleCoin") == "USDT" for _m, _p, q, _b in reads if q["category"] == "linear" and q.get("orderFilter"))      # the account-wide linear reads (not the per-symbol ones venue_state also makes)
