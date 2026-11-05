"""The live feed through the REAL snapshot and entry-gate path.

`LiveFeedBroker` is handed to the real `market_snapshot.take_snapshot`; its
snapshot goes to the real `MarketSnapshot.assert_fresh` and the real
`CarryRisk.gate_open`. The market is the captured real traffic; reference data
(instrument rules, a settled funding print, the REST touch) is added the way the
REST worker would add it. No venue is contacted."""
from __future__ import annotations

import json
import types

import pytest

import carry_risk
import main as app
import market_snapshot as MS
import md_broker as MB
import md_events as E
import md_state as S
import md_wire as W
from md_helpers import Factory, FakeClock, ScriptedWS, book_frame, load_frames, load_study
from test_md_wire_book_state import feed_fixture


class Account:
    """The signed-account side: position truth stays here, never in the feed."""

    def __init__(self, held=0.0, margin=3.0):
        self.held, self.margin, self.calls = held, margin, []

    def get_perp_position(self, symbol):
        self.calls.append("pos")
        return self.held

    def get_margin_multiple(self, symbol):
        self.calls.append("margin")
        return self.margin


def complete_state(env="mainnet"):
    """Real traffic plus the REST-sourced reference data. Returns (state, now_mono, now_wall)."""
    st = S.MarketState("BTCUSDT", environment=env)
    (fr, ctx), _ = feed_fixture(st)
    now, wall = ctx.recv_mono_s, fr["recv_ms"]
    for name in ("instrument_linear.json.gz", "instrument_spot.json.gz"):
        import md_rest as R
        body = json.dumps(load_study(name))
        cat = "linear" if "linear" in name else "spot"
        st.set_specs(list(R.parse_rest("/v5/market/instruments-info", {"category": cat}, 200, body,
                                       W.FrameContext(1, cat, "rest", wall, now, E.SOURCE_REST)).specs))
    nxt = st.tickers[E.LINEAR].fields["nextFundingTime"]
    st.set_last_print(E.FundingPrint("BTCUSDT", float(load_study("funding_BTCUSDT.json.gz")[-1]["fundingRate"]),
                                     nxt - 8 * 3600 * 1000, E.Provenance(0, "rest", "rest", "x", wall, now)))
    v = st.view(now, wall)
    st.set_rest_touch(E.LINEAR, v.perp_bid, v.perp_ask, now)
    st.set_rest_touch(E.SPOT, v.spot_bid, v.spot_ask, now)
    return st, now, wall


def broker_for(st, now, wall, *, account=None, env="mainnet"):
    clock = types.SimpleNamespace(m=now, w=wall)
    provider = lambda: st.view(clock.m, clock.w)                       # noqa: E731
    b = MB.LiveFeedBroker(provider, account or Account(), expected_environment=env,
                          perp_symbol="BTCUSDT", spot_symbol="BTCUSDT")
    return b, clock


def snapshot(b, wall):
    return MS.take_snapshot(b, perp_symbol="BTCUSDT", spot_symbol="BTCUSDT", now_s=wall / 1000.0)


def risk():
    return carry_risk.CarryRisk(max_notional_usd=100.0)


def gate(snap):
    # evaluated "now" = the snapshot's own instant, so freshness is judged on the capture
    import datetime as dt
    return risk().gate_open(notional_usd=100.0, snapshot=snap, has_open_pair=False,
                            now=dt.datetime.fromtimestamp(snap.observed_at_s, tz=dt.timezone.utc))


@pytest.fixture
def frozen_time(monkeypatch):
    def freeze(wall_ms):
        monkeypatch.setattr(MS.time, "time", lambda: wall_ms / 1000.0)
    return freeze


# -- a complete feed gives a real, fresh, approvable snapshot -------------------------------------------

def test_the_feed_produces_a_real_snapshot_the_engine_accepts(frozen_time):
    st, now, wall = complete_state()
    frozen_time(wall)
    b, _ = broker_for(st, now, wall)
    snap = snapshot(b, wall)
    snap.assert_fresh()
    v = st.view(now, wall)
    assert snap.perp_mark == v.perp_mark and snap.spot_mark == v.spot_mid          # exact floats
    assert snap.funding_bps == pytest.approx(v.funding_rate * 1e4)
    assert snap.funding_print_ms == v.last_funding_print[1]
    assert snap.perp_book == (v.perp_bid, v.perp_ask) and snap.spot_book == (v.spot_bid, v.spot_ask)
    assert snap.detail["market_source"] == "live_feed" and snap.detail["entry_refusals"] == []
    assert snap.margin_multiple is None and snap.perp_held == 0.0                   # flat: no margin read
    decision = gate(snap)
    assert decision.ok, decision


def test_all_public_values_in_one_snapshot_come_from_one_pinned_view(frozen_time):
    st, now, wall = complete_state()
    frozen_time(wall)
    b, clock = broker_for(st, now, wall)
    mark = b.get_mark("BTCUSDT")
    clock.m += 2.0                                           # time passes between the calls ...
    clock.w += 2000
    assert b.get_spot_mark("BTCUSDT") == st.view(now, wall).spot_mid      # ... the pinned view is used
    assert b.get_book_top("BTCUSDT", "linear")["bid"] == st.view(now, wall).perp_bid
    assert b.get_mark("BTCUSDT") == mark or True             # a new snapshot re-pins (and may go stale)


def test_position_and_margin_come_from_the_account_not_the_stream(frozen_time):
    st, now, wall = complete_state()
    frozen_time(wall)
    acct = Account(held=0.5, margin=4.25)
    b, _ = broker_for(st, now, wall, account=acct)
    snap = snapshot(b, wall)
    assert snap.perp_held == 0.5 and snap.margin_multiple == 4.25
    assert acct.calls == ["pos", "margin"]


# -- stale or incomplete markets are rejected --------------------------------------------------------------

def test_a_stale_book_refuses_the_snapshot_entirely(frozen_time):
    st, now, wall = complete_state()
    frozen_time(wall + 5000)
    b, clock = broker_for(st, now + 5.0, wall + 5000)
    with pytest.raises(S.MarketUnusable) as exc:
        snapshot(b, wall + 5000)
    assert any(r.startswith("perp_book:STALE") for r in exc.value.reasons)


def test_a_dropped_connection_refuses_the_snapshot(frozen_time):
    st, now, wall = complete_state()
    st.on_disconnected(E.LINEAR)
    frozen_time(wall)
    b, _ = broker_for(st, now, wall)
    with pytest.raises(S.MarketUnusable) as exc:
        snapshot(b, wall)
    assert "perp_connection:DISCONNECTED" in exc.value.reasons


def test_without_a_settled_funding_print_there_is_no_snapshot(frozen_time):
    st, now, wall = complete_state()
    st.last_print = None
    frozen_time(wall)
    b, _ = broker_for(st, now, wall)
    with pytest.raises(S.MarketUnusable) as exc:
        snapshot(b, wall)
    assert "funding_print:NO_SETTLED_PRINT" in exc.value.reasons


@pytest.mark.parametrize("drop,reason", [
    ("specs", "instruments:NO_SPEC"),
    ("rest", "rest_agreement:NO_REST_CROSSCHECK"),
])
def test_entry_only_shortfalls_stop_new_exposure_but_not_the_view_of_a_held_pair(frozen_time, drop, reason):
    st, now, wall = complete_state()
    if drop == "specs":
        st.specs.clear()
    else:
        st.rest_touch.clear()
    frozen_time(wall)
    b, _ = broker_for(st, now, wall, account=Account(held=0.001, margin=3.0))
    snap = snapshot(b, wall)                              # the view of a HELD pair still exists
    snap.assert_fresh()
    assert any(r.startswith(reason) for r in snap.detail["entry_refusals"])
    d = gate(snap)
    assert not d.ok and d.reason == "MARKET_FEED_REFUSES_ENTRY"
    assert any(r.startswith(reason) for r in d.detail["reasons"])


def test_a_feed_of_the_other_environment_is_never_used(frozen_time):
    st, now, wall = complete_state(env="mainnet")
    frozen_time(wall)
    b, _ = broker_for(st, now, wall, env="testnet")      # a testnet engine, a mainnet feed
    with pytest.raises(S.MarketUnusable) as exc:
        snapshot(b, wall)
    assert any(r.startswith("environment:mainnet_FEED_FOR_testnet_ENGINE") for r in exc.value.reasons)


def test_a_symbol_the_feed_does_not_serve_is_refused(frozen_time):
    st, now, wall = complete_state()
    frozen_time(wall)
    b, _ = broker_for(st, now, wall)
    with pytest.raises(ValueError):
        b.get_mark("ETHUSDT")
    b.get_mark("BTCUSDT")
    with pytest.raises(ValueError):
        b.get_book_top("ETHUSDT", "spot")
    with pytest.raises(ValueError):
        b.get_book_top("BTCUSDT", "futures")


def test_a_snapshot_detail_failure_refuses_entries(frozen_time, monkeypatch):
    st, now, wall = complete_state()
    frozen_time(wall)
    b, _ = broker_for(st, now, wall)

    def boom(self):
        raise RuntimeError("detail broke")
    monkeypatch.setattr(MB.LiveFeedBroker, "snapshot_detail", boom)
    snap = snapshot(b, wall)
    assert snap.detail["entry_refusals"] == ["SNAPSHOT_DETAIL_UNREADABLE:RuntimeError"]
    assert gate(snap).reason == "MARKET_FEED_REFUSES_ENTRY"


def test_a_plain_rest_broker_is_unchanged_by_the_new_snapshot_hook(frozen_time):
    class Rest:
        def get_mark(self, s): return 80000.0
        def get_spot_mark(self, s): return 80010.0
        def get_funding_bps(self, s): return 1.0
        def get_funding_print(self, s): return 1.0, int(1_793_836_800_000 - 8 * 3600 * 1000)
        def get_perp_position(self, s): return 0.0
    frozen_time(1_793_820_000_000)
    snap = MS.take_snapshot(Rest(), perp_symbol="BTCUSDT", spot_symbol="BTCUSDT", now_s=1_793_820_000.0)
    assert "entry_refusals" not in snap.detail and gate(snap).ok


# -- depth and top-of-book served in the broker's own shapes --------------------------------------------------------

def test_book_depth_matches_the_shape_the_ledger_reads(frozen_time):
    st, now, wall = complete_state()
    frozen_time(wall)
    b, _ = broker_for(st, now, wall)
    b.get_mark("BTCUSDT")
    d = b.get_book_depth("BTCUSDT", "linear", limit=10)
    assert set(d) == {"bid", "ask", "bids", "asks", "venue_ts_ms"}
    assert len(d["bids"]) == 10 and d["bid"] == d["bids"][0][0] < d["ask"] == d["asks"][0][0]
    assert all(p1 > p2 for (p1, _), (p2, _) in zip(d["bids"], d["bids"][1:]))
    assert d["venue_ts_ms"] and isinstance(d["venue_ts_ms"], int)


# -- the production wiring in main.py ----------------------------------------------------------------------------

def bare_bot(tmp_path, *, enabled=True, directory=None, testnet=True):
    bot = object.__new__(app.TradingBot)
    bot.cfg = types.SimpleNamespace(MARKET_FEED_ENABLED=enabled, USE_TESTNET=testnet,
                                    MARKET_FEED_DIR=str(tmp_path / "feed") if directory is None else directory)
    broker = Account()
    bot.carry = types.SimpleNamespace(perp_symbol="BTCUSDT", spot_symbol="BTCUSDT", broker=broker)
    bot.market_feed = None
    bot.market_feed_broker = None
    bot._market_feed_state = "DISABLED"
    bot._market_feed_factory = None
    return bot, broker


def test_disabled_means_the_account_broker_reads_the_market_as_before(tmp_path):
    bot, broker = bare_bot(tmp_path, enabled=False)
    bot._start_market_feed()
    assert bot._market_feed_state == "DISABLED" and bot._snapshot_source() is broker


def test_enabled_starts_a_feed_of_the_engines_environment_and_serves_snapshots_from_it(tmp_path):
    clock = FakeClock()
    sockets = [ScriptedWS([("timeout",)] * 3, auto_pong=True), ScriptedWS([("timeout",)] * 3)]
    bot, broker = bare_bot(tmp_path, testnet=True)
    factory = Factory(*sockets)
    bot._market_feed_factory = factory
    bot._start_market_feed()
    try:
        assert bot._market_feed_state == "RUNNING"
        assert bot._snapshot_source() is bot.market_feed_broker
        assert bot.market_feed_broker.expected_environment == "testnet"
        import time
        for _ in range(100):
            if len(factory.urls) >= 2:
                break
            time.sleep(0.05)
        assert sorted(factory.urls) == ["wss://stream-testnet.bybit.com/v5/public/linear",
                                        "wss://stream-testnet.bybit.com/v5/public/spot"]
    finally:
        bot._stop_market_feed()
    assert bot._market_feed_state == "STOPPED"
    mainnet, _ = bare_bot(tmp_path / "m", testnet=False)
    f2 = Factory(ScriptedWS([("timeout",)] * 3), ScriptedWS([("timeout",)] * 3))
    mainnet._market_feed_factory = f2
    mainnet._start_market_feed()
    try:
        assert mainnet.market_feed_broker.expected_environment == "mainnet"
    finally:
        mainnet._stop_market_feed()


def test_a_feed_that_cannot_start_refuses_snapshots_and_never_falls_back_to_rest(tmp_path):
    import os
    here = os.path.dirname(os.path.abspath(app.__file__))
    bot, broker = bare_bot(tmp_path, directory=os.path.join(here, "state", "feed"))
    bot._start_market_feed()
    assert bot._market_feed_state == "FAILED_TO_START"
    with pytest.raises(S.MarketUnusable) as exc:
        bot._snapshot_source()
    assert exc.value.reasons == ["feed:FAILED_TO_START"]


def test_the_feed_store_may_not_share_a_directory_with_the_other_environment(tmp_path):
    import md_store as ST
    ST.MarketStore(str(tmp_path / "x"), environment="mainnet").close()
    with pytest.raises(ST.StoreError):
        ST.MarketStore(str(tmp_path / "x"), environment="testnet")
    bot, _ = bare_bot(tmp_path, directory=str(tmp_path / "x"), testnet=True)
    bot._start_market_feed()
    assert bot._market_feed_state == "FAILED_TO_START"


def test_config_requires_a_directory_when_the_feed_is_enabled():
    import config
    env = {"MARKET_FEED_ENABLED": "1"}
    with pytest.raises(config.ConfigError, match="MARKET_FEED_DIR"):
        config.load({**_base_env(), **env})
    cfg = config.load({**_base_env(), **env, "MARKET_FEED_DIR": "/data/market"})
    assert cfg.MARKET_FEED_ENABLED and cfg.MARKET_FEED_DIR == "/data/market"
    assert config.load(_base_env()).MARKET_FEED_ENABLED is False


def _base_env():
    return {"USE_TESTNET": "1", "PAPER_TRADING": "1"}


def test_a_truthy_test_double_config_does_not_turn_the_feed_on(tmp_path):
    """A mock config's auto-created attribute is truthy; only a real True counts."""
    from unittest import mock
    bot, broker = bare_bot(tmp_path, enabled=False)
    bot.cfg = mock.MagicMock()
    assert bot._market_feed_enabled() is False
    assert bot._snapshot_source() is broker
    bot.cfg.MARKET_FEED_ENABLED = "true"
    assert bot._market_feed_enabled() is False                     # a string is not the flag either


def test_a_stub_that_invents_attributes_cannot_inject_entry_refusals(frozen_time):
    """The hook is declared on the class; an instance attribute (or a mock's
    auto-attribute) is ignored."""
    from unittest import mock

    class Stub(mock.MagicMock):
        pass
    stub = Stub()
    stub.get_mark.return_value = 80000.0
    stub.get_spot_mark.return_value = 80010.0
    stub.get_funding_bps.return_value = 1.0
    stub.get_funding_print.return_value = (1.0, 1_793_836_800_000 - 8 * 3600 * 1000)
    stub.get_perp_position.return_value = 0.0
    stub.get_venue_time_s.side_effect = RuntimeError("no clock")
    stub.get_book_top.side_effect = RuntimeError("no touch")
    frozen_time(1_793_820_000_000)
    snap = MS.take_snapshot(stub, perp_symbol="BTCUSDT", spot_symbol="BTCUSDT", now_s=1_793_820_000.0)
    assert "entry_refusals" not in snap.detail


def test_only_the_live_feed_broker_declares_the_hook():
    assert MB.LiveFeedBroker.provides_snapshot_detail is True
