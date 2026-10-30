"""The liquidation view carries the venue's positionIM when it states one."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from carry_broker import CarryBroker                          # noqa: E402
from test_carry_broker import FakeClient, envelope            # noqa: E402

POS = {"side": "Sell", "size": "0.001", "positionIdx": 0, "markPrice": "100000",
       "positionIM": "20", "positionMM": "5", "liqPrice": "130000"}


def view(row):
    client = FakeClient(mark=100_000.0)
    real = client._request

    def request(method, endpoint, **kw):
        if endpoint == "/v5/position/list":
            return envelope({"list": [dict(row)]})
        return real(method, endpoint, **kw)
    client._request = request
    b = CarryBroker(client=client, order_gate=lambda: (True, "T"),
                    sequence_source=lambda *a: 1)
    return b.get_liquidation_view("BTCUSDT")


def test_a_stated_initial_margin_is_in_the_view():
    assert view(POS)["position_im_usd"] == pytest.approx(20.0)


def test_an_unreachable_liquidation_price_still_reports_the_margin():
    assert view(dict(POS, liqPrice=""))["position_im_usd"] == pytest.approx(20.0)


@pytest.mark.parametrize("bad", [None, "", "nan", "-3", "abc"])
def test_an_unstated_or_unusable_margin_is_none_and_never_blocks_the_view(bad):
    row = dict(POS)
    row["positionIM"] = bad
    v = view(row)
    assert v["position_im_usd"] is None
    assert v["distance_pct"] == pytest.approx(30.0)      # the gate is unaffected
