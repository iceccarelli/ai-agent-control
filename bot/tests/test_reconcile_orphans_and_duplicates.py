"""Lost create-replies and venue-only positions must not become invisible.

Invariants protected (audit S2/S3/S7):
  * a POST /v5/order/create whose reply is lost and whose retry is rejected as
    a duplicate orderLinkId is recorded from the exchange's answer, never as
    'rejected';
  * rows left 'unknown' by an exhausted retry loop are chased at startup;
  * a linear position the venue holds but the ledger does not know refuses
    startup instead of being silently ignored.
"""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import bybit_connection as bc  # noqa: E402
import main as m  # noqa: E402
import trading_engine as te  # noqa: E402
from fake_bybit import FakeBybit  # noqa: E402
from persistence import StateStore  # noqa: E402
from position_sizing import BillionairePositionSizing  # noqa: E402
from risk_management import BillionaireRiskManager  # noqa: E402
from test_orchestrator import Cfg  # noqa: E402


class LinearCfg(Cfg):
    CATEGORY = "linear"
    ALLOW_SHORTS = True


class LossyTransport:
    """Forwards to FakeBybit but loses the reply of the first order create."""

    def __init__(self, inner):
        self.inner = inner
        self.lost = 0

    def request(self, method, url, **kw):
        status, text = self.inner.request(method, url, **kw)
        if method == "POST" and url.endswith("/v5/order/create") and self.lost == 0:
            self.lost += 1
            raise ConnectionError("reply lost after the exchange accepted")
        return status, text


@pytest.fixture()
def store(tmp_path):
    s = StateStore(str(tmp_path / "state.db"))
    s.claim_writer()
    s.update_equity(10_000.0)
    return s


def test_duplicate_link_id_after_lost_reply_is_recorded_from_the_exchange(store):
    fake = FakeBybit()
    lossy = LossyTransport(fake)
    client = bc.BybitClient(config=Cfg(), store=store, transport=lossy)
    result = client.place_order(symbol="BTCUSDT", side="Buy", qty=0.001)
    assert lossy.lost == 1
    assert result.ok is True, result
    assert result.reason == "SUBMITTED"
    row = store.get_order(result.order_link_id)
    assert row["status"] in {"submitted", "filled"}
    assert row["exchange_id"]
    # exactly ONE order exists at the venue - nothing was resubmitted
    assert len(fake.orders) == 1


def test_duplicate_looks_like_helper_matches_code_and_message():
    assert bc.BybitClient._looks_like_duplicate_link_id(
        bc.PermanentAPIError("x", ret_code=110072))
    assert bc.BybitClient._looks_like_duplicate_link_id(
        bc.PermanentAPIError("Duplicate orderLinkId", ret_code=999))
    assert not bc.BybitClient._looks_like_duplicate_link_id(
        bc.PermanentAPIError("insufficient balance", ret_code=110007))


def test_unknown_rows_are_chased_at_startup(store):
    fake = FakeBybit()
    client = bc.BybitClient(config=Cfg(), store=store, transport=fake)
    res = client.place_order(symbol="BTCUSDT", side="Buy", qty=0.001)
    assert res.ok
    # simulate an exhausted retry loop having left the row 'unknown'
    store.update_order_status(res.order_link_id, "unknown")
    assert res.order_link_id not in {r["order_link_id"] for r in store.pending_orders()}
    assert res.order_link_id in {r["order_link_id"] for r in store.unresolved_orders()}
    summary = client.reconcile_on_startup()
    assert summary["checked"] == 1
    assert summary["unknown"] == 0
    assert store.get_order(res.order_link_id)["status"] != "unknown"


def _bot(tmp_path, cfg, fake):
    store = StateStore(str(tmp_path / "state.db"))
    store.update_equity(10_000.0)
    client = bc.BybitClient(config=cfg, store=store, transport=fake)
    risk = BillionaireRiskManager(config=cfg, store=store)
    engine = te.TradingEngine(
        client=client, risk_manager=risk,
        position_sizer=BillionairePositionSizing(config=cfg, risk_manager=risk),
        store=store, config=cfg,
    )
    return m.TradingBot(config=cfg, store=store, client=client,
                        risk_manager=risk, engine=engine, strategy=None), store


def test_venue_position_unknown_to_ledger_refuses_startup(tmp_path):
    fake = FakeBybit()
    fake.positions["BTCUSDT"] = {"symbol": "BTCUSDT", "side": "Sell", "size": "0.01",
                                 "avgPrice": "60000", "stopLoss": ""}
    bot, _ = _bot(tmp_path, LinearCfg(), fake)
    assert bot.startup() is False
    bot.shutdown()


def test_venue_position_known_to_ledger_does_not_refuse(tmp_path):
    fake = FakeBybit()
    fake.positions["BTCUSDT"] = {"symbol": "BTCUSDT", "side": "Sell", "size": "0.01",
                                 "avgPrice": "60000", "stopLoss": "61000"}
    bot, store = _bot(tmp_path, LinearCfg(), fake)
    store.claim_writer()
    store.upsert_position("BTCUSDT", "Sell", 0.01, 60000.0, stop_price=61000.0)
    store.release_writer() if hasattr(store, "release_writer") else None
    summary = bot.client.reconcile_on_startup(symbols=("BTCUSDT",))
    assert summary["orphan_positions"] == []
    bot.shutdown()


def test_spot_reconcile_never_queries_position_list(tmp_path):
    fake = FakeBybit()
    bot, _ = _bot(tmp_path, Cfg(), fake)
    bot.client.reconcile_on_startup(symbols=("BTCUSDT",))
    assert not any(r["url"].endswith("/v5/position/list") for r in fake.requests)
    bot.shutdown()
