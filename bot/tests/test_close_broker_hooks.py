"""The two broker hooks the staged close needs, and how main builds the executor."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from carry_broker import CarryBroker, PairIncident            # noqa: E402
from test_carry_broker import FakeClient, broker as mk_broker, envelope   # noqa: E402


class PosClient(FakeClient):
    def __init__(self, rows, **kw):
        super().__init__(**kw)
        self.rows = rows

    def _request(self, method, endpoint, **kw):
        if endpoint == "/v5/position/list":
            self.calls.append((method, endpoint, kw.get("params"), kw.get("body")))
            return envelope({"list": self.rows})
        return super()._request(method, endpoint, **kw)


def row(side, size, idx="0"):
    return {"side": side, "size": str(size), "positionIdx": idx,
            "positionIM": "1", "positionMM": "1"}


# -- signed net position --------------------------------------------------------------

def test_a_short_reads_negative_and_flat_reads_zero():
    assert mk_broker(PosClient([row("Sell", 0.012)])).get_perp_net_qty("BTCUSDT") == pytest.approx(-0.012)
    assert mk_broker(PosClient([row("", 0)])).get_perp_net_qty("BTCUSDT") == 0.0
    assert mk_broker(PosClient([])).get_perp_net_qty("BTCUSDT") == 0.0


def test_a_long_is_seen_not_refused_so_a_reversal_can_be_halted_on():
    assert mk_broker(PosClient([row("Buy", 0.3)])).get_perp_net_qty("BTCUSDT") == pytest.approx(0.3)


def test_hedge_mode_long_slot_and_unstated_side_are_unreadable():
    with pytest.raises(PairIncident):
        mk_broker(PosClient([row("Buy", 0.3, "1")])).get_perp_net_qty("BTCUSDT")
    with pytest.raises(PairIncident):
        mk_broker(PosClient([row("", 0.3)])).get_perp_net_qty("BTCUSDT")


def test_a_non_list_answer_is_unreadable_not_flat():
    c = PosClient([])
    c._request = lambda *a, **k: envelope({"unexpected": 1})
    with pytest.raises(PairIncident):
        mk_broker(c).get_perp_net_qty("BTCUSDT")


# -- caller-supplied order id -----------------------------------------------------------------

def test_a_supplied_link_id_is_the_one_on_the_wire_and_the_close_is_reduce_only():
    c = FakeClient()
    mk_broker(c).place_market(symbol="BTCUSDT", side="Buy", qty=0.01, product="linear",
                              order_link_id="CL-cyc-1-1")
    assert c.created[0]["orderLinkId"] == "CL-cyc-1-1" and c.created[0]["reduceOnly"] is True


@pytest.mark.parametrize("bad", ["", "a b", "x" * 37, "id/1", "id;1", "é"])
def test_a_malformed_link_id_is_refused_before_anything_is_sent(bad):
    c = FakeClient()
    with pytest.raises(ValueError):
        mk_broker(c).place_market(symbol="BTCUSDT", side="Buy", qty=0.01,
                                  product="linear", order_link_id=bad)
    assert c.created == []


def test_without_a_supplied_id_the_minted_one_is_used_as_before():
    c = FakeClient()
    mk_broker(c).place_market(symbol="BTCUSDT", side="Buy", qty=0.01, product="linear")
    assert c.created[0]["orderLinkId"]


# -- main wiring ---------------------------------------------------------------------------------

def cfg(**kw):
    base = dict(CARRY_CLOSE_MAX_ATTEMPTS="3", CARRY_CLOSE_MAX_WAITS="12",
                CARRY_CLOSE_MAX_LAG_WAITS="3", CARRY_CLOSE_RESEND="NEVER",
                CARRY_CLOSE_MAX_SENDS="", CARRY_CLOSE_ORDER_TYPE="MARKET_REDUCE_ONLY",
                CARRY_CLOSE_ON_TIMEOUT="HALT", CARRY_CLOSE_RESIDUAL="HALT_AND_RECORD",
                CARRY_CLOSE_ESCALATION="HALT_AND_LOG_CRITICAL",
                CARRY_CLOSE_SLIPPAGE_CEILING_BPS="", CARRY_CLOSE_PATH_APPROVED="APPROVED_BY_OWNER",
                LOOP_INTERVAL_SECONDS=60.0, REQUEST_TIMEOUT_SECONDS=10.0,
                BYBIT_RECV_WINDOW_MS=5000, CARRY_PERP_SYMBOL="BTCUSDT")
    base.update(kw)
    return SimpleNamespace(**base)


def build(c):
    import main
    return main.build_close_executor(c, store=object(), broker=SimpleNamespace(inner=object()))


def test_nothing_set_means_no_executor():
    assert build(SimpleNamespace(LOOP_INTERVAL_SECONDS=60.0)) is None


def test_values_set_but_not_approved_means_no_executor():
    assert build(cfg(CARRY_CLOSE_PATH_APPROVED="")) is None
    assert build(cfg(CARRY_CLOSE_PATH_APPROVED="yes")) is None


def test_a_slippage_ceiling_cannot_be_enforced_so_it_blocks_the_path():
    assert build(cfg(CARRY_CLOSE_SLIPPAGE_CEILING_BPS="50")) is None


def test_fully_set_and_approved_builds_an_executor():
    ex = build(cfg())
    assert ex is not None and ex.absence_window_s >= 60.0


def test_a_wait_limit_too_short_to_outlast_the_absence_window_is_refused():
    # 1 poll of 1 second can never prove a lost request absent
    assert build(cfg(CARRY_CLOSE_MAX_WAITS="1", LOOP_INTERVAL_SECONDS=1.0)) is None


def test_the_ledger_wrapper_passes_the_signed_position_and_the_supplied_id_through():
    import ledger
    seen = {}

    class Inner:
        def get_perp_net_qty(self, s):
            return -0.5

        def place_market(self, **kw):
            seen.update(kw)
            return {"filled_qty": 0.0, "avg_price": 0.0, "order_link_id": kw.get("order_link_id")}
    lb = ledger.LedgerBroker(Inner(), ledger.Journal(), ms=lambda: 1, spot_symbol="BTCUSDT",
                             book_overlay_inventory=True)
    assert lb.get_perp_net_qty("BTCUSDT") == -0.5
    lb.place_market(symbol="BTCUSDT", side="Buy", qty=0.01, product="linear",
                    order_link_id="CL-1")
    assert seen["order_link_id"] == "CL-1"
