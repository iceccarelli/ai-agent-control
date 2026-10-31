"""Pure verdicts of testnet_readiness: every named refusal, and comparability."""
from __future__ import annotations

import copy
import gzip
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import testnet_readiness as tr  # noqa: E402

H8 = 8 * 3600 * 1000
NOW = 1_790_000_000_000 // H8 * H8 + 60_000      # one minute after a print
HERE = os.path.dirname(os.path.abspath(__file__))


def _fixture(name):
    path = os.path.join(HERE, "..", "research", "exchange_study", "bybit", name)
    row = json.load(gzip.open(path))["result"]["list"][0]
    return row


def history(n=5, end=NOW - 60_000):
    return [(end - (n - 1 - i) * H8, 0.0001) for i in range(n)]


def good(**over):
    kw = dict(venue="testnet", use_testnet=True, live_trading=False,
              live_authorized=False, endpoint_host=tr.TESTNET_HOST,
              perp_instrument=_fixture("instrument_linear.json.gz"),
              spot_instrument=_fixture("instrument_spot.json.gz"),
              snapshot_ages_s={"perp_mark": 1.0, "spot_mark": 1.0, "funding": 2.0},
              venue_clock_offset_ms=120, position_mode="one_way",
              margin_mode="REGULAR_MARGIN", funding_history=history(),
              funding_required=True, now_ms=NOW)
    kw.update(over)
    return tr.assess_testnet_readiness(**kw)


def test_recorded_btcusdt_metadata_is_ready():
    v = good()
    assert v.ready, v.refusals


def test_verdict_is_frozen():
    with pytest.raises(Exception):
        good().ready = False


@pytest.mark.parametrize("over,code", [
    (dict(venue="mainnet", use_testnet=False, endpoint_host="api.bybit.com"),
     tr.ACCIDENTAL_MAINNET),
    (dict(use_testnet=False), tr.ACCIDENTAL_MAINNET),
    (dict(endpoint_host="api.bybit.com"), tr.ACCIDENTAL_MAINNET),
    (dict(live_trading=True), tr.LIVE_MODE),
    (dict(live_authorized=True), tr.LIVE_MODE),
    (dict(live_trading=None), tr.LIVE_MODE),
    (dict(venue="paper"), tr.UNRECOGNIZED_VENUE),
    (dict(venue=None), tr.UNRECOGNIZED_VENUE),
    (dict(use_testnet=None), tr.UNRECOGNIZED_VENUE),
    (dict(venue="demo", endpoint_host="api-demo.bybit.com"), tr.UNRECOGNIZED_VENUE),
    (dict(snapshot_ages_s={"perp_mark": 500.0, "spot_mark": 1.0, "funding": 1.0}),
     tr.STALE_MARKET_DATA),
    (dict(snapshot_ages_s={"perp_mark": float("nan"), "spot_mark": 1.0, "funding": 1.0}),
     tr.STALE_MARKET_DATA),
    (dict(snapshot_ages_s={"perp_mark": 1.0}), tr.STALE_MARKET_DATA),
    (dict(snapshot_ages_s=None), tr.STALE_MARKET_DATA),
    (dict(venue_clock_offset_ms=60_000), tr.EXCESSIVE_CLOCK_DRIFT),
    (dict(venue_clock_offset_ms=None), tr.EXCESSIVE_CLOCK_DRIFT),
    (dict(position_mode="hedge"), tr.INVALID_POSITION_MODE),
    (dict(position_mode=None), tr.INVALID_POSITION_MODE),
    (dict(margin_mode=None), tr.INVALID_MARGIN_MODE),
    (dict(perp_instrument=None), tr.UNVERIFIED_PRECISION),
    (dict(funding_history=None), tr.INCOMPLETE_FUNDING_HISTORY),
    (dict(funding_history=history(2)), tr.INCOMPLETE_FUNDING_HISTORY),
    (dict(funding_history=history(5, end=NOW - 3 * H8)), tr.INCOMPLETE_FUNDING_HISTORY),
])
def test_named_refusals(over, code):
    v = good(**over)
    assert not v.ready and v.has(code), v.refusals


def test_funding_history_not_required_is_not_checked():
    assert good(funding_history=None, funding_required=False).ready


def test_funding_gap_duplicate_unit_and_grid():
    h = history()
    assert tr.check_funding_history(h[:2] + h[3:], interval_s=28800, now_ms=NOW, min_prints=3)
    dup = h[:3] + [h[2]] + h[3:]
    assert tr.check_funding_history(dup, interval_s=28800, now_ms=NOW, min_prints=3)
    bps = [(t, r * 1e4 * 100) for t, r in h]    # 1.0 per print
    assert any(x.code == tr.FUNDING_UNIT_SUSPECT for x in
               tr.check_funding_history(bps, interval_s=28800, now_ms=NOW, min_prints=3))
    off = [(t + 3_600_000, r) for t, r in h]
    assert tr.check_funding_history(off, interval_s=28800, now_ms=NOW + 3_600_000, min_prints=3)
    fut = h + [(h[-1][0] + H8, 0.0001)]
    assert tr.check_funding_history(fut, interval_s=28800, now_ms=NOW, min_prints=3)


@pytest.mark.parametrize("mut", [
    lambda r: r["priceFilter"].update(tickSize="0"),
    lambda r: r["lotSizeFilter"].update(qtyStep=""),
    lambda r: r["lotSizeFilter"].update(minOrderQty="0.0015"),     # not a step multiple
    lambda r: r["lotSizeFilter"].update(maxOrderQty="0.0001"),
    lambda r: r.update(status="Closed"),
    lambda r: r.update(contractType="LinearFutures"),
    lambda r: r.update(fundingInterval="abc"),
    lambda r: r.update(upperFundingRate="-1"),
])
def test_bad_perp_metadata_refused(mut):
    row = copy.deepcopy(_fixture("instrument_linear.json.gz"))
    mut(row)
    v = good(perp_instrument=row)
    assert not v.ready
    assert {tr.UNVERIFIED_PRECISION, tr.INCONSISTENT_CONTRACT_METADATA} & set(v.codes)


def test_perp_spot_mismatch_refused():
    spot = copy.deepcopy(_fixture("instrument_spot.json.gz"))
    spot["symbol"] = "ETHUSDT"
    spot["baseCoin"] = "ETH"
    assert good(spot_instrument=spot).has(tr.INCONSISTENT_CONTRACT_METADATA)


# -- comparability -----------------------------------------------------------

def series(venue="bybit_linear", unit="fraction", interval=28800, symbol="BTCUSDT",
           n=10, rate=0.0001, jitter=0, cap=None):
    base = 1_700_000_000_000 // (interval * 1000) * interval * 1000
    times = tuple(base + i * interval * 1000 + jitter for i in range(n))
    return tr.FundingSeriesDescriptor(venue, symbol, unit, interval, times,
                                      tuple(rate for _ in times), cap)


def test_same_venue_same_shape_is_exact():
    assert tr.assess_funding_comparability(series(), series()).level == "exact"


def test_binance_history_vs_bybit_runtime_is_incomparable_by_default():
    h = series(venue="binance_usdtm", jitter=5)
    v = tr.assess_funding_comparability(h, series())
    assert v.level == "incomparable" and "different venues" in v.reasons[0]
    p = tr.assess_funding_comparability(h, series(), allow_cross_venue_proxy=True)
    assert p.level == "proxy" and p.comparable


@pytest.mark.parametrize("hist,needle", [
    (series(unit="bps", rate=1.0), "unit mismatch"),
    (series(interval=14400), "interval mismatch"),
    (series(symbol="ETHUSDT"), "symbol mismatch"),
    (series(jitter=3_600_000), "off the"),
    (series(rate=1.0), "implausible"),
    (series(rate=0.005, cap=None), None),
])
def test_incomparable_reasons(hist, needle):
    v = tr.assess_funding_comparability(hist, series())
    if needle is None:
        assert v.level == "exact"
    else:
        assert v.level == "incomparable" and any(needle in r for r in v.reasons), v.reasons


def test_history_beyond_runtime_cap_incomparable():
    v = tr.assess_funding_comparability(series(rate=0.005), series(cap=0.00333))
    assert v.level == "incomparable" and any("cap" in r for r in v.reasons)


def test_irregular_cadence_incomparable():
    h = series()
    t = list(h.times_ms)
    del t[4]
    h2 = tr.FundingSeriesDescriptor(h.venue, h.symbol, h.unit, h.interval_s, tuple(t),
                                    tuple(0.0001 for _ in t))
    assert tr.assess_funding_comparability(h2, series()).level == "incomparable"


def test_unknown_unit_incomparable():
    assert tr.assess_funding_comparability(series(unit="pct"), series()).level == "incomparable"


def test_repo_binance_corpus_vs_bybit_runtime():
    """The shipped Binance BTC corpus is 8h/fraction; against Bybit it is a
    proxy only when acknowledged."""
    import csv
    p = os.path.join(HERE, "..", "data", "real_funding_full", "funding",
                     "BINANCE_LINEAR_BTC_USDT_FUNDING.csv.gz")
    rows = list(csv.DictReader(gzip.open(p, "rt")))[-200:]
    hist = tr.FundingSeriesDescriptor(
        "binance_usdtm", "BTCUSDT", "fraction", 28800,
        tuple(int(r["funding_time_ms"]) for r in rows),
        tuple(float(r["funding_rate"]) for r in rows))
    runtime = series(cap=0.00333)
    assert tr.assess_funding_comparability(hist, runtime).level == "incomparable"
    assert tr.assess_funding_comparability(hist, runtime, allow_cross_venue_proxy=True).comparable
