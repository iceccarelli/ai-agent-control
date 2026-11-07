"""fundingCap / funding interval / next settlement: what the REAL parse path (Bybit V5 frame -> md_wire -> md_events -> md_state ->
MarketView -> decision consumers) carries, and what it does not.

Established facts (each pinned below):
  * md_wire._ticker CARRIES fundingCap (validated >= 0), fundingRate, fundingIntervalHour and nextFundingTime into the Ticker event.
  * md_state projects fundingCap -> MarketView.funding_cap, fundingIntervalHour -> funding_interval_h, nextFundingTime -> next_funding_ms.
  * upperFundingRate / lowerFundingRate / fundingInterval(min) are INSTRUMENTS-INFO fields: md_rest -> InstrumentSpec only. A WS ticker never has
    them and the MarketView does not carry them.
  * The only md-path decision read of funding is LiveFeedBroker.get_funding_bps, which returns the forecast rate. No consumer reads
    view.funding_cap. The REST broker (carry_broker.get_funding_bps) refuses a rate beyond its own cap; the md path did not until md_state
    refused it.
Offline: no network, no state directory."""
from __future__ import annotations

import inspect
import json

import pytest

import md_broker as MB
import md_events as E
import md_state as S
import md_wire as W
from test_md_integration import broker_for, complete_state


def _ctx(now, wall, raw_id=9_000_001):
    return W.FrameContext(raw_id=raw_id, category=E.LINEAR, conn_id="t", recv_wall_ms=wall, recv_mono_s=now)


def _delta(st, now, wall, **data):
    """Feed one ticker delta for BTCUSDT linear through the real parser into the state; return the parsed Ticker event."""
    frame = {"topic": "tickers.BTCUSDT", "type": "delta", "ts": wall, "data": dict(symbol="BTCUSDT", **data)}
    parsed = W.parse_frame(json.dumps(frame), _ctx(now, wall))
    assert not parsed.rejections, parsed.rejections
    st.ingest(parsed, category=E.LINEAR)
    return parsed.events[0]


def test_wire_carries_cap_rate_interval_and_next_settlement_into_the_event():
    st, now, wall = complete_state()
    ev = _delta(st, now, wall, fundingCap="0.00333", fundingRate="0.0002", fundingIntervalHour="8",
                nextFundingTime=str(wall + 3_600_000))
    assert ev.fields["fundingCap"] == 0.00333 and ev.fields["fundingRate"] == 0.0002
    assert ev.fields["fundingIntervalHour"] == 8 and ev.fields["nextFundingTime"] == wall + 3_600_000


def test_state_projects_cap_interval_and_next_settlement_onto_the_view_but_not_upper_lower():
    st, now, wall = complete_state()
    _delta(st, now, wall, fundingCap="0.00333", fundingIntervalHour="8", nextFundingTime=str(wall + 3_600_000))
    v = st.view(now, wall)
    assert v.funding_cap == 0.00333 and v.funding_interval_h == 8 and v.next_funding_ms == wall + 3_600_000
    assert not hasattr(v, "upper_funding_rate") and not hasattr(v, "lower_funding_rate")


def test_upper_lower_and_interval_minutes_come_only_from_instruments_info():
    st, _now, _wall = complete_state()
    spec = st.specs[E.LINEAR]
    assert spec.funding_interval_min == 480
    assert spec.upper_funding_rate is not None and spec.lower_funding_rate is not None
    assert spec.upper_funding_rate > 0 > spec.lower_funding_rate


@pytest.mark.parametrize("bad", ["-0.1", "abc", "1_0", "nan"])
def test_hostile_funding_cap_is_rejected_by_the_wire(bad):
    _st, now, wall = complete_state()
    frame = {"topic": "tickers.BTCUSDT", "type": "delta", "ts": wall, "data": {"symbol": "BTCUSDT", "fundingCap": bad}}
    p = W.parse_frame(json.dumps(frame), _ctx(now, wall))
    assert p.rejections and not p.events


def test_no_decision_consumer_reads_the_projected_cap():
    """Pinned honestly: md economics, capacity and the broker read funding_rate/interval, never funding_cap. If a consumer starts to, this
    fails and the new reader needs its own test."""
    import md_capacity
    import md_economics
    for mod in (md_economics, md_capacity, MB):
        assert "funding_cap" not in inspect.getsource(mod), mod.__name__


def test_positive_control_a_rate_inside_its_cap_is_priced():
    st, now, wall = complete_state()
    _delta(st, now, wall, fundingCap="0.00333", fundingRate="0.0002")
    b, _ = broker_for(st, now, wall)
    assert b.get_funding_bps("BTCUSDT") == pytest.approx(2.0)


@pytest.mark.parametrize("rate", ["0.0100", "-0.0100"])
def test_a_rate_beyond_the_cap_in_the_same_ticker_never_reaches_the_decision(rate):
    """Reproduced defect (failing before the md_state fix): the REST broker refuses |rate| > fundingCap as a unit slip / wrong field; the md
    path priced it (100 bps) into the entry decision."""
    st, now, wall = complete_state()
    _delta(st, now, wall, fundingCap="0.00333", fundingRate=rate)
    b, _ = broker_for(st, now, wall)
    with pytest.raises(S.MarketUnusable) as ei:
        b.get_funding_bps("BTCUSDT")
    assert "FUNDING_BEYOND_CAP" in str(ei.value)


def test_a_zero_cap_is_unusable_not_a_cap_of_zero():
    st, now, wall = complete_state()
    _delta(st, now, wall, fundingCap="0", fundingRate="0.0001")
    b, _ = broker_for(st, now, wall)
    with pytest.raises(S.MarketUnusable) as ei:
        b.get_funding_bps("BTCUSDT")
    assert "FUNDING_CAP_UNUSABLE" in str(ei.value)
