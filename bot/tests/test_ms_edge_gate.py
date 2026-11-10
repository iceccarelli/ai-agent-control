"""The expected-NET-edge gate: a funding rate is not a profit. Built on the one economic model (economic_model.evaluate_chain)."""
from __future__ import annotations

import os
import sys
from decimal import Decimal as D

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (HERE, os.path.dirname(HERE), os.path.join(os.path.dirname(HERE), "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import economic_model as EM  # noqa: E402
import ms_edge_gate as EG  # noqa: E402
from ms_testnet_world import World  # noqa: E402

NOW = 1_794_312_000_000
PERP_ROW = {"symbol": "BTCUSDT", "status": "Trading", "baseCoin": "BTC", "quoteCoin": "USDT", "fundingInterval": "480", "upperFundingRate": "0.0075", "lowerFundingRate": "-0.0075",
            "priceFilter": {"tickSize": "0.1"}, "lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001", "minNotionalValue": "5"}}
SPOT_ROW = {"symbol": "BTCUSDT", "status": "Trading", "baseCoin": "BTC", "quoteCoin": "USDT", "priceFilter": {"tickSize": "0.01"},
            "lotSizeFilter": {"basePrecision": "0.000001", "minOrderAmt": "1"}}


def book(mid="75000", half="0.05", depth="20", ts=NOW):
    m, h, q = D(mid), D(half), D(depth)
    return EM.Book(bids=((m - h, q), (m - h - 1, q)), asks=((m + h, q), (m + h + 1, q)), ts_ms=ts)


def inputs(rate="0.0001", measured=True, **over):
    kw = dict(symbol="BTCUSDT", notional_usd=D("375"), now_ms=NOW, funding_rate=D(rate), next_funding_ms=NOW + 600_000, hold_s=720, perp_row=PERP_ROW, spot_row=SPOT_ROW,
              perp_book=book(), spot_book=book(), leverage=1,
              perp_fee=(D("0.0002"), D("0.00055")) if measured else None, spot_fee=(D("0.001"), D("0.001")) if measured else None, fee_observed_ms=NOW if measured else None)
    kw.update(over)
    return EG.EdgeInputs(**kw)


def test_the_typical_testnet_funding_of_one_bp_is_uneconomic_and_the_required_funding_is_derived():
    v = EG.evaluate_edge(inputs("0.0001"))
    assert v.session_class == EG.REJECTED and v.settlements_in_hold == 1
    assert D(v.net_edge_bps) < 0 and D(v.expected_net_usd) < 0                       # about -31 bps of fees alone against 0.7 bps of expected funding
    assert D(v.costs_bps["fees"]) == D("31")                                         # spot 0.1% + perp 0.055%, entry and exit
    assert D(v.required_funding_bps) > D("40")                                       # the funding that WOULD pay: derived from the chain, not typed in
    assert any(r.startswith("NET_EDGE_") for r in v.reasons)


def test_the_required_funding_is_the_exact_boundary_between_rejected_and_profit_candidate():
    base = EG.evaluate_edge(inputs("0.0001"))
    req = D(base.required_funding_bps) / EG.BPS                                      # fraction per settlement
    above = EG.evaluate_edge(inputs(str(req * D("1.02"))))
    below = EG.evaluate_edge(inputs(str(req * D("0.98"))))
    assert above.session_class == EG.PROFIT_CANDIDATE and below.session_class == EG.REJECTED
    assert D(above.net_edge_bps) >= EG.MIN_NET_EDGE_BPS > D(below.net_edge_bps)


def test_an_assumed_fee_tier_can_never_pass_even_when_the_funding_is_huge():
    v = EG.evaluate_edge(inputs("0.0100", measured=False))                            # 100 bps: gross alone would clear every cost
    assert v.session_class == EG.INCONCLUSIVE and "fees" in v.assumed_inputs
    assert any(r.startswith("ASSUMED_INPUTS_CANNOT_PASS") for r in v.reasons) and D(v.net_edge_bps) > 0


def test_a_stale_fee_read_is_not_current():
    v = EG.evaluate_edge(inputs("0.0100", fee_observed_ms=NOW - 2 * EG.MAX_FEE_AGE_MS))
    assert v.session_class == EG.INCONCLUSIVE and v.net_edge_bps is None


def test_negative_funding_is_a_negative_expectation_for_a_short():
    v = EG.evaluate_edge(inputs("-0.0005"))
    assert v.session_class == EG.REJECTED and D(v.expected_funding_bps) < 0


def test_no_settlement_inside_the_hold_is_no_funding_at_all():
    v = EG.evaluate_edge(inputs("0.0100", next_funding_ms=NOW + 4_000_000, hold_s=720))
    assert v.session_class == EG.REJECTED and v.reasons == ("NO_SETTLEMENT_INSIDE_THE_HOLD",)


@pytest.mark.parametrize("over,why", [
    ({"perp_book": book(depth="0.0001")}, "BOOK_TOO_THIN"),
    ({"spot_book": book(ts=NOW - 60_000)}, "STALE_BOOK"),
    ({"perp_row": dict(PERP_ROW, status="Settling")}, "NOT_TRADING"),
])
def test_a_thin_stale_or_untradable_market_is_inconclusive_never_a_pass(over, why):
    v = EG.evaluate_edge(inputs("0.0100", **over))
    assert v.session_class == EG.INCONCLUSIVE and any(why in r for r in v.reasons)


def test_a_malformed_instrument_row_is_inconclusive():
    v = EG.evaluate_edge(inputs("0.0100", perp_row={"symbol": "BTCUSDT"}))
    assert v.session_class == EG.INCONCLUSIVE and v.reasons[0].startswith("INSTRUMENT_INVALID")


def test_the_verdict_is_deterministic_and_carries_a_digest():
    a, b = EG.evaluate_edge(inputs("0.0050")), EG.evaluate_edge(inputs("0.0050"))
    assert a == b and a.digest == b.digest and EG.evaluate_edge(inputs("0.0051")).digest != a.digest


def test_the_hurdle_is_positive_and_not_lowerable_by_the_caller():
    assert EG.MIN_NET_EDGE_BPS > 0 and EG.FUNDING_HAIRCUT > 0
    v = EG.evaluate_edge(inputs("0.0100"))
    assert v.session_class == EG.PROFIT_CANDIDATE and D(v.net_edge_bps) >= EG.MIN_NET_EDGE_BPS


# ---------------------------------------------------------------- reading the venue (fake exchange)
@pytest.fixture
def w(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def _ex(w):
    import ms_testnet_transport as TT
    import ms_testnet_executor as TE
    client, _t = TT.build_client(TT.TestnetCredentials.from_mapping(w.env), w.fake)
    return TE.TestnetExecutor(client, symbols=["BTCUSDT"], spot_symbols=["BTCUSDT"], clock_ms=w.clock.now, kill_reader=lambda: (False, "t"))


def test_collect_reads_instruments_books_and_the_account_fee_tier(w):
    w.fake.fee_rates = {"spot": ("0.001", "0.001"), "linear": ("0.0002", "0.00055")}
    inp = EG.collect(_ex(w), "BTCUSDT", D("375"), 720, D("0.0001"), w.clock.now() + 600_000, w.clock.now())
    assert inp.perp_fee == (D("0.0002"), D("0.00055")) and inp.fee_observed_ms == w.clock.now()
    v = EG.evaluate_edge(inp)
    assert v.session_class == EG.REJECTED and v.assumed_inputs == () and D(v.costs_bps["fees"]) == D("31")


def test_collect_falls_back_to_an_assumed_tier_when_the_account_fee_read_is_refused(w):
    w.fake.fee_rates = None
    inp = EG.collect(_ex(w), "BTCUSDT", D("375"), 720, D("0.0001"), w.clock.now() + 600_000, w.clock.now())
    assert inp.perp_fee is None and inp.fee_observed_ms is None
    v = EG.evaluate_edge(inp)
    assert "fees" in v.assumed_inputs and v.session_class != EG.PROFIT_CANDIDATE
