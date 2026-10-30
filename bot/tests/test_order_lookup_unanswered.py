"""A lookup the venue refuses to answer is not an answer that the order is absent.

`get_order(strict=True)` returned None, and `reconcile_on_startup` then wrote the
terminal status `never_sent`, whenever the venue rejected the READ with an
authentication, permission, whitelist or rate-limit error. An order whose fate
is unknown was erased from the unresolved set with no other symptom. Only the
venue's own "no such order" codes mean absence. Found by the independent review
(Agent 6, finding 1) and reproduced here first. Offline; a faked transport.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bybit_connection as bc  # noqa: E402
from fake_bybit import FakeBybit  # noqa: E402
from persistence import StateStore  # noqa: E402
from test_bybit_connection import Cfg  # noqa: E402


@pytest.fixture()
def store(tmp_path):
    s = StateStore(str(tmp_path / "s.db"))
    yield s
    s.close()


@pytest.fixture()
def client(store):
    return bc.BybitClient(config=Cfg(), store=store, transport=FakeBybit())


def refuse_reads_with(client, code):
    real = client._request

    def request(method, endpoint, **kw):
        if method == "GET" and endpoint.startswith("/v5/order/"):
            raise bc.PermanentAPIError(f"venue said {code}", ret_code=code)
        return real(method, endpoint, **kw)
    client._request = request


@pytest.mark.parametrize("code", [10003, 10004, 10005, 10010, 10016, 10018, 99999])
def test_a_refused_lookup_is_not_an_answer_that_the_order_is_absent(client, code):
    refuse_reads_with(client, code)
    with pytest.raises(bc.PermanentAPIError):
        client.get_order(symbol="BTCUSDT", order_link_id="X-1", strict=True)


@pytest.mark.parametrize("code", [110001, 170213])
def test_the_venues_own_no_such_order_codes_still_mean_absent(client, code):
    refuse_reads_with(client, code)
    assert client.get_order(symbol="BTCUSDT", order_link_id="X-1", strict=True) is None


@pytest.mark.parametrize("code", [10003, 10005, 10010])
def test_reconcile_leaves_an_unknown_order_unknown_when_the_read_is_refused(
        client, store, code):
    store.record_order("X-9", "BTCUSDT", "Buy", "Market", 0.001, purpose="entry",
                       status="unknown")
    refuse_reads_with(client, code)
    summary = client.reconcile_on_startup(["BTCUSDT"])
    assert store.get_order("X-9")["status"] == "unknown"
    assert summary["unknown"] == 1 and summary["resolved"] == 0


def test_reconcile_still_marks_an_order_the_venue_does_not_have_as_never_sent(
        client, store):
    store.record_order("X-8", "BTCUSDT", "Buy", "Market", 0.001, purpose="entry",
                       status="submitted")
    summary = client.reconcile_on_startup(["BTCUSDT"])
    assert store.get_order("X-8")["status"] == "never_sent"
    assert summary["resolved"] == 1


def test_the_not_found_codes_are_exactly_the_ones_the_repo_already_treats_as_gone():
    assert bc.ORDER_NOT_FOUND_RET_CODES == frozenset({110001, 170213})
    assert bc.ORDER_NOT_FOUND_RET_CODES.isdisjoint(bc.UNKNOWN_OUTCOME_RET_CODES)
