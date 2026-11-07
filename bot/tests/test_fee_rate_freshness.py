"""The account fee tier is read from the venue, not served forever: a cached rate expires, an expired rate that cannot be re-read RAISES,
a maker rebate is a real tier (accepted, bounded) and a negative TAKER rate is never accepted."""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import carry_broker as CB  # noqa: E402
from test_funding_reconciliation import SYM, Venue, broker, envelope  # noqa: E402


class FeeVenue(Venue):
    def __init__(self):
        super().__init__()
        self.maker, self.taker, self.calls, self.fail = "0.0002", "0.00055", 0, False

    def _request(self, method, endpoint, **kw):
        if endpoint == "/v5/account/fee-rate":
            self.calls += 1
            if self.fail:
                raise RuntimeError("venue unreachable")
            return envelope({"category": "linear", "list": [{"symbol": SYM, "makerFeeRate": self.maker, "takerFeeRate": self.taker}]})
        return super()._request(method, endpoint, **kw)


@pytest.fixture()
def clock(monkeypatch):
    t = {"now": 1000.0}
    monkeypatch.setattr(CB.time, "monotonic", lambda: t["now"])
    return t


def test_a_rate_inside_the_ttl_is_served_from_cache_and_after_it_the_new_tier_is_read(clock):
    v = FeeVenue(); b = broker(v)
    assert b.get_fee_rates(SYM, "linear") == pytest.approx({"maker_bps": 2.0, "taker_bps": 5.5})
    v.maker, v.taker = "0.0001", "0.0004"
    clock["now"] += CB.CarryBroker._FEE_RATE_TTL_S - 1
    assert b.get_fee_rates(SYM, "linear")["taker_bps"] == pytest.approx(5.5) and v.calls == 1
    clock["now"] += 2
    assert b.get_fee_rates(SYM, "linear") == pytest.approx({"maker_bps": 1.0, "taker_bps": 4.0}) and v.calls == 2


def test_an_expired_rate_that_cannot_be_re_read_raises_instead_of_serving_the_stale_price(clock):
    v = FeeVenue(); b = broker(v)
    b.get_fee_rates(SYM, "linear")
    clock["now"] += CB.CarryBroker._FEE_RATE_TTL_S + 1
    v.fail = True
    with pytest.raises(Exception):
        b.get_fee_rates(SYM, "linear")


def test_a_maker_rebate_is_accepted_and_a_negative_taker_or_absurd_rebate_is_not(clock):
    v = FeeVenue(); v.maker = "-0.00010"
    assert broker(v).get_fee_rates(SYM, "linear") == pytest.approx({"maker_bps": -1.0, "taker_bps": 5.5})
    for maker, taker in (("0.0001", "-0.0001"), ("-0.0050", "0.00055"), ("nan", "0.00055")):
        v2 = FeeVenue(); v2.maker, v2.taker = maker, taker
        with pytest.raises(CB.PairIncident):
            broker(v2).get_fee_rates(SYM, "linear")
