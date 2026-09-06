"""Contract tests for the Bybit V5 client, run entirely offline.

Every test here corresponds to something the legacy client got wrong. The three
API-shape facts asserted below were verified against Bybit's own documentation
in July 2026:

  * /v5/position/trading-stop does NOT support category=spot
  * the TP/SL mode parameter is spelled `tpslMode`
  * private WS auth signs "GET/realtime" + expires (epoch ms)
"""
from __future__ import annotations

import ast
import hashlib
import hmac
import json
import os
import sys
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bybit_connection as bc  # noqa: E402
from fake_bybit import API_KEY, API_SECRET, FakeBybit  # noqa: E402
from persistence import StateStore  # noqa: E402


class Cfg:
    """Minimal config double so tests do not depend on ambient env."""

    USE_TESTNET = True
    BYBIT_API_KEY = API_KEY
    BYBIT_API_SECRET = API_SECRET
    BYBIT_RECV_WINDOW_MS = 5000
    REQUEST_TIMEOUT_SECONDS = 5.0
    USE_LEVERAGE = False
    SYMBOL_FILTERS_TTL = 3600
    ORDERLINK_PREFIX = "BB"


@pytest.fixture()
def exchange():
    return FakeBybit()


@pytest.fixture()
def store(tmp_path):
    s = StateStore(str(tmp_path / "s.db"))
    yield s
    s.close()


@pytest.fixture()
def client(exchange, store):
    return bc.BybitClient(config=Cfg(), store=store, transport=exchange)


# ---------------------------------------------------------------------------
# signing and clock
# ---------------------------------------------------------------------------


class TestSigning:
    def test_signed_request_is_accepted_by_a_strict_verifier(self, client, exchange):
        """The fake rejects a bad signature, so a pass here is real evidence."""
        assert client.get_equity() == pytest.approx(10_000.0)

    def test_get_signs_the_query_string_that_is_actually_sent(self, client, exchange):
        client.get_open_orders(symbol="BTCUSDT")
        req = exchange.requests[-1]
        assert "?" in req["url"]
        query = req["url"].split("?", 1)[1]
        expected = hmac.new(
            API_SECRET.encode(),
            f"{req['headers']['X-BAPI-TIMESTAMP']}{API_KEY}"
            f"{req['headers']['X-BAPI-RECV-WINDOW']}{query}".encode(),
            hashlib.sha256,
        ).hexdigest()
        assert req["headers"]["X-BAPI-SIGN"] == expected

    def test_post_signs_the_exact_body_bytes_that_are_sent(self, client, exchange):
        client.place_order(symbol="BTCUSDT", side="Buy", qty=0.001)
        req = [r for r in exchange.requests if r["url"].endswith("/order/create")][-1]
        expected = hmac.new(
            API_SECRET.encode(),
            f"{req['headers']['X-BAPI-TIMESTAMP']}{API_KEY}"
            f"{req['headers']['X-BAPI-RECV-WINDOW']}{req['body']}".encode(),
            hashlib.sha256,
        ).hexdigest()
        assert req["headers"]["X-BAPI-SIGN"] == expected

    def test_post_body_is_not_empty(self, client, exchange):
        """N2: every legacy POST was signed over "{}" and sent with no params,
        because the body was passed positionally into the `params` slot."""
        client.place_order(symbol="BTCUSDT", side="Buy", qty=0.001)
        body = json.loads(
            [r for r in exchange.requests if r["url"].endswith("/order/create")][-1]["body"]
        )
        for key in ("category", "symbol", "side", "orderType", "qty", "orderLinkId"):
            assert body.get(key), f"POST body missing {key}"

    def test_sign_type_header_is_present(self, client, exchange):
        client.get_equity()
        assert exchange.requests[-1]["headers"]["X-BAPI-SIGN-TYPE"] == "2"

    def test_timestamp_is_epoch_ms_not_perf_counter(self, client, exchange):
        """perf_counter() was used as the signing clock in the legacy module."""
        import time as _t

        client.get_equity()
        ts = int(exchange.requests[-1]["headers"]["X-BAPI-TIMESTAMP"])
        now_ms = int(_t.time() * 1000)
        assert abs(ts - now_ms) < 60_000, (
            f"timestamp {ts} is not epoch-ms (now {now_ms}); "
            "perf_counter would be ~seconds since process start"
        )

    def test_signing_without_a_secret_raises(self, exchange, store):
        class NoCreds(Cfg):
            BYBIT_API_KEY = ""
            BYBIT_API_SECRET = ""

        c = bc.BybitClient(config=NoCreds(), store=store, transport=exchange)
        with pytest.raises(bc.PermanentAPIError):
            c.get_equity()

    def test_clock_skew_is_measured_and_applied(self, client):
        skew = client.sync_time()
        assert isinstance(skew, int)
        assert abs(skew) < 60_000


# ---------------------------------------------------------------------------
# instrument filters  (audit C14)
# ---------------------------------------------------------------------------


class TestInstrumentFilters:
    def test_filters_come_from_the_exchange(self, client):
        f = client.get_instrument_filters("BTCUSDT")
        assert f.from_exchange is True
        assert f.tick_size == Decimal("0.01")
        assert f.qty_step == Decimal("0.000001")

    def test_spot_uses_basePrecision_as_the_quantity_step(self, client):
        """Spot exposes basePrecision; qtyStep is a linear/inverse field."""
        assert client.get_instrument_filters("ADAUSDT").qty_step == Decimal("0.1")

    def test_per_symbol_filters_actually_differ(self, client):
        """The legacy client returned the same hardcoded set for every symbol."""
        btc = client.get_instrument_filters("BTCUSDT")
        ada = client.get_instrument_filters("ADAUSDT")
        shib = client.get_instrument_filters("SHIBUSDT")
        assert len({btc.qty_step, ada.qty_step, shib.qty_step}) == 3
        assert len({btc.tick_size, ada.tick_size, shib.tick_size}) == 3

    def test_sub_cent_tick_is_preserved(self, client):
        assert client.get_instrument_filters("SHIBUSDT").tick_size == Decimal("0.00000001")

    def test_unknown_symbol_raises_rather_than_defaulting(self, client):
        with pytest.raises(bc.PermanentAPIError):
            client.get_instrument_filters("NOTREALUSDT")

    def test_filters_are_cached_within_ttl(self, client, exchange):
        client.get_instrument_filters("BTCUSDT")
        before = len(exchange.requests)
        client.get_instrument_filters("BTCUSDT")
        assert len(exchange.requests) == before, "TTL cache not used"

    def test_force_refresh_bypasses_the_cache(self, client, exchange):
        client.get_instrument_filters("BTCUSDT")
        before = len(exchange.requests)
        client.get_instrument_filters("BTCUSDT", force=True)
        assert len(exchange.requests) > before

    def test_no_hardcoded_filter_literals_in_source(self):
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "bybit_connection.py",
        )
        src = open(path, encoding="utf-8").read()
        for ln in src.splitlines():
            if ln.lstrip().startswith("#") or "``" in ln:
                continue
            assert '"tickSize":0.01' not in ln.replace(" ", "")
            assert '"minNotional":5.0' not in ln.replace(" ", "")


# ---------------------------------------------------------------------------
# idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_order_link_id_is_deterministic_for_the_same_intent_and_seq(self):
        a = bc.build_order_link_id(seq=7, symbol="BTCUSDT", side="Buy", qty="0.001")
        b = bc.build_order_link_id(seq=7, symbol="BTCUSDT", side="Buy", qty="0.001")
        assert a == b

    def test_order_link_id_differs_across_intents(self):
        a = bc.build_order_link_id(seq=7, symbol="BTCUSDT", side="Buy", qty="0.001")
        b = bc.build_order_link_id(seq=7, symbol="BTCUSDT", side="Sell", qty="0.001")
        assert a != b

    def test_order_link_id_respects_the_36_character_limit(self):
        oid = bc.build_order_link_id(
            seq=999_999_999, symbol="VERYLONGSYMBOLUSDT", side="Buy",
            qty="0.000000001", purpose="stoploss", prefix="PREFIX",
        )
        assert len(oid) <= 36

    def test_sequence_survives_a_restart(self, tmp_path, exchange):
        """The property that makes a post-crash retry safe."""
        db = str(tmp_path / "s.db")
        s1 = StateStore(db)
        c1 = bc.BybitClient(config=Cfg(), store=s1, transport=exchange)
        c1.place_order(symbol="BTCUSDT", side="Buy", qty=0.001)
        first = sorted(exchange.orders)[0]
        s1.close()

        s2 = StateStore(db)
        c2 = bc.BybitClient(config=Cfg(), store=s2, transport=exchange)
        c2.place_order(symbol="BTCUSDT", side="Buy", qty=0.001)
        assert len(exchange.orders) == 2, "restart must not reuse the same sequence"
        assert first in exchange.orders
        s2.close()

    def test_replaying_the_same_link_id_does_not_double_submit(self, client, exchange):
        first = client.place_order(symbol="BTCUSDT", side="Buy", qty=0.001)
        assert first.ok
        again = client.place_order(
            symbol="BTCUSDT", side="Buy", qty=0.001,
            order_link_id=first.order_link_id,
        )
        assert again.ok is False
        assert again.reason == "DUPLICATE_INTENT"
        assert len(exchange.orders) == 1, "a duplicate intent reached the exchange"

    def test_intent_is_recorded_before_submission(self, client, store, exchange):
        """So a crash between record and ack is recoverable."""
        result = client.place_order(symbol="BTCUSDT", side="Buy", qty=0.001)
        assert store.get_order(result.order_link_id) is not None

    def test_exchange_rejects_a_duplicate_link_id(self, client, exchange):
        """Belt and braces: even if our guard failed, the exchange refuses."""
        result = client.place_order(symbol="BTCUSDT", side="Buy", qty=0.001)
        body = json.loads(exchange.requests[-1]["body"])
        status, text = exchange.request(
            "POST", "https://api-testnet.bybit.com/v5/order/create",
            headers={}, body=json.dumps(body),
        )
        assert json.loads(text)["retCode"] == 170130


# ---------------------------------------------------------------------------
# spot-correct protective stops  (audit C11)
# ---------------------------------------------------------------------------


class TestSpotStops:
    def test_stop_is_a_conditional_order_not_a_trading_stop_call(
        self, client, exchange
    ):
        result = client.place_stop_order(
            symbol="BTCUSDT", side="Sell", qty=0.001, trigger_price=49_000.0
        )
        assert result.ok
        assert not any(
            "position/trading-stop" in r["url"] for r in exchange.requests
        ), "spot stop was posted to a derivatives-only endpoint"
        body = json.loads(
            [r for r in exchange.requests if r["url"].endswith("/order/create")][-1]["body"]
        )
        assert body["orderFilter"] == "StopOrder"
        assert body["triggerPrice"]
        assert body["triggerDirection"] == 2

    def test_a_spot_stop_never_touches_the_trading_stop_endpoint(
        self, client, exchange
    ):
        """Verified against the docs: that endpoint is linear/inverse/option only.

        Slice 7 added a linear category, which *does* legitimately use
        ``/v5/position/trading-stop``. So the old source-grep for that string no
        longer proves anything — the guard has to be behavioural instead: on a
        SPOT client the endpoint must never be reached, whatever the source
        contains.
        """
        assert client.is_spot
        client.place_stop_order(
            symbol="BTCUSDT", side="Sell", qty=0.001, trigger_price=49_000.0
        )
        urls = [r["url"] for r in exchange.requests]
        assert not any("position/trading-stop" in u for u in urls), urls
        assert any("order/create" in u for u in urls), urls

    def test_the_spot_stop_is_a_conditional_stop_order(self, client, exchange):
        """The spot mechanism, stated as an assertion rather than a comment."""
        client.place_stop_order(
            symbol="BTCUSDT", side="Sell", qty=0.001, trigger_price=49_000.0
        )
        body = json.loads(exchange.requests[-1]["body"])
        assert body["orderFilter"] == "StopOrder"
        assert body["category"] == "spot"
        assert "triggerPrice" in body and "triggerDirection" in body

    def test_long_stop_triggers_on_a_fall(self, client, exchange):
        client.place_stop_order(
            symbol="BTCUSDT", side="Sell", qty=0.001, trigger_price=49_000.0
        )
        assert json.loads(exchange.requests[-1]["body"])["triggerDirection"] == 2

    def test_short_stop_triggers_on_a_rise(self, client, exchange):
        client.balances = getattr(client, "balances", None)
        exchange.balances["USDT"] = 1_000_000.0
        client.place_stop_order(
            symbol="BTCUSDT", side="Buy", qty=0.001, trigger_price=51_000.0
        )
        assert json.loads(exchange.requests[-1]["body"])["triggerDirection"] == 1

    def test_stop_trigger_price_is_snapped_to_the_tick(self, client, exchange):
        client.place_stop_order(
            symbol="BTCUSDT", side="Sell", qty=0.001, trigger_price=49_000.567_89
        )
        trigger = json.loads(exchange.requests[-1]["body"])["triggerPrice"]
        assert Decimal(trigger) % Decimal("0.01") == 0

    def test_stop_below_min_qty_is_refused(self, client):
        result = client.place_stop_order(
            symbol="BTCUSDT", side="Sell", qty=0.0000001, trigger_price=49_000.0
        )
        assert result.ok is False
        assert result.reason == "STOP_QTY_BELOW_MIN"

    def test_take_profit_is_a_resting_limit_order(self, client, exchange):
        result = client.place_take_profit(
            symbol="BTCUSDT", side="Sell", qty=0.001, limit_price=52_000.0
        )
        assert result.ok
        body = json.loads(exchange.requests[-1]["body"])
        assert body["orderType"] == "Limit"
        assert body["timeInForce"] == "GTC"
        assert body["price"]


# ---------------------------------------------------------------------------
# quantisation and the never-bump rule
# ---------------------------------------------------------------------------


class TestQuantisation:
    def test_quantity_is_snapped_down_to_the_step(self, client, exchange):
        client.place_order(symbol="ADAUSDT", side="Buy", qty=15.97)
        assert Decimal(json.loads(exchange.requests[-1]["body"])["qty"]) == Decimal("15.9")

    def test_sub_minimum_sell_is_skipped_not_upgraded_to_sell_everything(
        self, client, exchange
    ):
        """Audit C15: the legacy path rewrote a small sell into a full liquidation."""
        result = client.place_order(symbol="BTCUSDT", side="Sell", qty=0.0000001)
        assert result.ok is False
        assert result.reason in {"QTY_BELOW_MIN", "QTY_BELOW_STEP"}
        assert exchange.orders == {}, "an order was sent for a sub-minimum sell"

    def test_below_min_notional_is_refused(self, client):
        result = client.place_order(symbol="BTCUSDT", side="Buy", qty=0.00005)
        assert result.ok is False
        assert result.reason in {"BELOW_MIN_NOTIONAL", "QTY_BELOW_MIN"}

    def test_limit_price_is_snapped_to_the_tick(self, client, exchange):
        client.place_order(
            symbol="BTCUSDT", side="Buy", qty=0.001,
            order_type="Limit", price=50_000.567_89,
        )
        price = json.loads(exchange.requests[-1]["body"])["price"]
        assert Decimal(price) % Decimal("0.01") == 0

    def test_limit_without_price_is_refused(self, client):
        result = client.place_order(
            symbol="BTCUSDT", side="Buy", qty=0.001, order_type="Limit"
        )
        assert result.reason == "LIMIT_WITHOUT_PRICE"

    def test_market_order_states_its_qty_unit(self, client, exchange):
        """Without marketUnit a spot market BUY is read as a quote amount."""
        client.place_order(symbol="BTCUSDT", side="Buy", qty=0.001)
        assert json.loads(exchange.requests[-1]["body"])["marketUnit"] == "baseCoin"


# ---------------------------------------------------------------------------
# balance and leverage  (audit C16)
# ---------------------------------------------------------------------------


class TestBalanceAndLeverage:
    def test_insufficient_quote_balance_rejects_a_buy(self, client, exchange):
        exchange.balances["USDT"] = 1.0
        result = client.place_order(symbol="BTCUSDT", side="Buy", qty=0.01)
        assert result.ok is False
        assert result.reason.startswith("INSUFFICIENT_USDT")
        assert exchange.orders == {}

    def test_insufficient_base_balance_rejects_a_sell(self, client, exchange):
        """Selling more base than is held is refused — and named correctly.

        It used to come back as ``INSUFFICIENT_BTC``, which is true but tells
        the operator nothing about *why* it can never work. On spot, a sell
        beyond the held balance is not an underfunded trade, it is a short —
        and spot cannot short. The reason now says so, and says what to change.
        """
        exchange.balances["BTC"] = 0.0
        result = client.place_order(symbol="BTCUSDT", side="Sell", qty=0.001)
        assert result.ok is False
        assert result.reason.startswith("SHORT_NOT_AVAILABLE_ON_SPOT")
        assert "CATEGORY=linear" in result.reason

    def test_selling_what_is_actually_held_still_works(self, client, exchange):
        """The refusal above must not block a legitimate exit."""
        exchange.balances["BTC"] = 1.0
        result = client.place_order(symbol="BTCUSDT", side="Sell", qty=0.001)
        assert result.ok is True, result.reason

    def test_no_infinite_balance_anywhere_in_source(self):
        """The legacy check set usdt_balance = float('inf') in BOTH branches."""
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "bybit_connection.py",
        )
        src = open(path, encoding="utf-8").read()
        offenders = [
            ln.strip() for ln in src.splitlines()
            if "float('inf')" in ln.replace('"', "'")
            and not ln.lstrip().startswith("#") and "``" not in ln
        ]
        assert offenders == []

    def test_is_leverage_is_zero_when_leverage_is_disabled(self, client, exchange):
        client.place_order(symbol="BTCUSDT", side="Buy", qty=0.001)
        assert json.loads(exchange.requests[-1]["body"])["isLeverage"] == 0

    def test_is_leverage_follows_config_not_a_hardcoded_true(
        self, exchange, store
    ):
        class Margin(Cfg):
            USE_LEVERAGE = True

        c = bc.BybitClient(config=Margin(), store=store, transport=exchange)
        c.place_order(symbol="BTCUSDT", side="Buy", qty=0.001)
        assert json.loads(exchange.requests[-1]["body"])["isLeverage"] == 1

    def test_unreadable_balance_blocks_the_order(self, client, exchange):
        exchange.scripted = [(500, "boom")] * 8
        result = client.place_order(symbol="BTCUSDT", side="Buy", qty=0.001)
        assert result.ok is False

    def test_equity_uses_total_equity_not_available_balance(self, client, exchange):
        exchange.equity = 12_345.0
        assert client.get_equity() == pytest.approx(12_345.0)


# ---------------------------------------------------------------------------
# error classification and retries
# ---------------------------------------------------------------------------


class TestRetryClassification:
    def test_permanent_error_is_not_retried(self, client, exchange):
        exchange.reject_next_with = (110003, "insufficient balance")
        before = len(exchange.requests)
        result = client.place_order(symbol="BTCUSDT", side="Buy", qty=0.001)
        creates = [
            r for r in exchange.requests[before:]
            if r["url"].endswith("/order/create")
        ]
        assert result.ok is False
        assert len(creates) == 1, "a permanent error was retried"

    def test_rate_limit_is_retried(self, client, exchange):
        exchange.scripted = [
            (200, json.dumps({"retCode": 10006, "retMsg": "rate limit", "result": {}}))
        ]
        assert client.get_equity() == pytest.approx(10_000.0)

    def test_transient_http_is_retried(self, client, exchange):
        exchange.scripted = [(503, "unavailable"), (502, "bad gateway")]
        assert client.get_equity() == pytest.approx(10_000.0)

    def test_unknown_outcome_is_not_resubmitted(self, client, exchange, store):
        """A timed-out submit must never be re-sent blind.

        Warm the filter and price caches first so the scripted timeouts land on
        `/v5/order/create` rather than on the lookups that precede it.
        """
        client.get_instrument_filters("BTCUSDT")
        before_orders = dict(exchange.orders)
        exchange.scripted = [(504, "gateway timeout")] * 10

        result = client.place_order(symbol="BTCUSDT", side="Buy", qty=0.001)

        # Whichever pre-flight call the timeout lands on, the answer is the
        # same: no order, and nothing silently retried into existence.
        assert result.ok is False
        assert exchange.orders == before_orders

    def test_a_timed_out_create_leaves_the_intent_unresolved(
        self, client, exchange, store
    ):
        """The intent must stay on the books for reconciliation, not be dropped."""
        client.get_instrument_filters("BTCUSDT")
        client.get_last_price("BTCUSDT")
        client.get_equity()

        # Let the pre-flight lookups succeed, then fail only the create.
        real_request = exchange.request
        state = {"creates": 0}

        def flaky(method, url, **kw):
            if url.endswith("/v5/order/create"):
                state["creates"] += 1
                return 504, "gateway timeout"
            return real_request(method, url, **kw)

        exchange.request = flaky
        result = client.place_order(symbol="BTCUSDT", side="Buy", qty=0.001)

        assert result.reason == "OUTCOME_UNKNOWN"
        assert state["creates"] > 1, "transient failures should be retried"
        row = store.get_order(result.order_link_id)
        assert row is not None, "the intent must survive for reconciliation"
        assert row["status"] == "unknown"

    def test_garbage_response_does_not_become_a_trade(self, client, exchange):
        exchange.scripted = [(200, "<html>not json</html>")]
        with pytest.raises(bc.PermanentAPIError):
            client.get_equity()

    def test_auth_failure_is_permanent(self, client, exchange):
        exchange.scripted = [(401, "unauthorised")]
        with pytest.raises(bc.PermanentAPIError):
            client.get_equity()

    def test_transient_code_table_excludes_permanent_conditions(self):
        """Legacy retried 110001 (no such order) and 110003 (no balance)."""
        assert 110001 not in bc.TRANSIENT_RET_CODES
        assert 110003 not in bc.TRANSIENT_RET_CODES
        assert 10006 in bc.TRANSIENT_RET_CODES


# ---------------------------------------------------------------------------
# klines: closed candles only
# ---------------------------------------------------------------------------


class TestKlines:
    def test_klines_are_oldest_first_and_drop_the_open_candle(self, client):
        rows = client.get_klines("BTCUSDT", limit=5)
        assert len(rows) == 4, "the in-progress candle was not dropped"
        timestamps = [int(r[0]) for r in rows]
        assert timestamps == sorted(timestamps), "klines are not oldest-first"


# ---------------------------------------------------------------------------
# reconciliation  (audit C18)
# ---------------------------------------------------------------------------


class TestReconciliation:
    def test_a_never_sent_intent_is_resolved(self, client, store):
        store.record_order("BB-entry-99-deadbeef", "BTCUSDT", "Buy", "Market", 0.001)
        summary = client.reconcile_on_startup()
        assert summary["resolved"] == 1
        assert store.get_order("BB-entry-99-deadbeef")["status"] == "never_sent"

    def test_a_live_order_is_marked_still_open(self, client, store, exchange):
        result = client.place_order(
            symbol="BTCUSDT", side="Buy", qty=0.001,
            order_type="Limit", price=40_000.0,
        )
        store.update_order_status(result.order_link_id, "pending")
        summary = client.reconcile_on_startup()
        assert summary["still_open"] == 1

    def test_a_filled_order_is_resolved(self, client, store, exchange):
        result = client.place_order(symbol="BTCUSDT", side="Buy", qty=0.001)
        store.update_order_status(result.order_link_id, "pending")
        summary = client.reconcile_on_startup()
        assert summary["resolved"] == 1
        assert store.get_order(result.order_link_id)["status"] == "filled"

    def test_naked_positions_are_reported(self, client, store):
        store.upsert_position("ETHUSDT", "Buy", 1.0, 3_000.0, stop_price=0.0)
        assert client.reconcile_on_startup()["naked_positions"] == ["ETHUSDT"]

    def test_unresolvable_orders_are_counted_not_assumed_dead(
        self, client, store, exchange
    ):
        store.record_order("BB-entry-1-aaaa", "BTCUSDT", "Buy", "Market", 0.001)
        exchange.scripted = [(500, "boom")] * 20
        summary = client.reconcile_on_startup()
        assert summary["unknown"] == 1


# ---------------------------------------------------------------------------
# private websocket auth  (audit C17)
# ---------------------------------------------------------------------------


class TestWebsocketAuth:
    def test_auth_signs_get_realtime_plus_expires(self, client):
        msg = client.ws_auth_message(expires_ms=1_700_000_000_000)
        expected = hmac.new(
            API_SECRET.encode(), b"GET/realtime1700000000000", hashlib.sha256
        ).hexdigest()
        assert msg["op"] == "auth"
        assert msg["args"] == [API_KEY, 1_700_000_000_000, expected]

    def test_expires_is_in_the_future_and_epoch_ms(self, client):
        import time as _t

        expires = client.ws_auth_message()["args"][1]
        now_ms = int(_t.time() * 1000)
        assert expires > now_ms
        assert expires - now_ms < 60_000

    def test_auth_without_credentials_raises(self, exchange, store):
        class NoCreds(Cfg):
            BYBIT_API_KEY = ""
            BYBIT_API_SECRET = ""

        c = bc.BybitClient(config=NoCreds(), store=store, transport=exchange)
        with pytest.raises(bc.PermanentAPIError):
            c.ws_auth_message()

    def test_ping_frame_shape(self, client):
        assert client.ws_ping_message() == {"op": "ping"}

    def test_private_ws_url_follows_testnet_flag(self, client, exchange, store):
        assert client.ws_private_url == bc.TESTNET_WS_PRIVATE

        class Main(Cfg):
            USE_TESTNET = False

        c = bc.BybitClient(config=Main(), store=store, transport=exchange)
        assert c.ws_private_url == bc.MAINNET_WS_PRIVATE
        assert c.base_url == bc.MAINNET_REST


# ---------------------------------------------------------------------------
# cancel / amend  (N2: these could never work before)
# ---------------------------------------------------------------------------


class TestCancel:
    def test_cancel_sends_a_populated_body(self, client, exchange):
        result = client.place_order(
            symbol="BTCUSDT", side="Buy", qty=0.001,
            order_type="Limit", price=40_000.0,
        )
        assert client.cancel_order(
            symbol="BTCUSDT", order_link_id=result.order_link_id
        ) is True
        body = json.loads(exchange.requests[-1]["body"])
        assert body["orderLinkId"] == result.order_link_id
        assert body["category"] == "spot"

    def test_cancelling_an_unknown_order_is_treated_as_already_gone(self, client):
        assert client.cancel_order(symbol="BTCUSDT", order_link_id="nope") is True

    def test_cancel_all_marks_resting_orders_cancelled(self, client, exchange):
        client.place_order(symbol="BTCUSDT", side="Buy", qty=0.001,
                           order_type="Limit", price=40_000.0)
        assert client.cancel_all("BTCUSDT") is True
        assert all(o["orderStatus"] == "Cancelled" for o in exchange.orders.values())


# ---------------------------------------------------------------------------
# structure
# ---------------------------------------------------------------------------


class TestStructure:
    def test_order_result_defaults_to_not_ok(self):
        """Legacy returned {'retCode': 0, 'success': False, 'skipped': True} —
        a shape that reads as success to anything checking retCode."""
        assert bc.OrderResult().ok is False
        assert bool(bc.OrderResult()) is False

    def test_no_bogus_imports(self):
        """`from decimal import _dec` is an unconditional ImportError; the
        legacy module had four of them, each killing an order path."""
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "bybit_connection.py",
        )
        tree = ast.parse(open(path, encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "decimal":
                names = {a.name for a in node.names}
                assert "_dec" not in names, "from decimal import _dec always fails"
        # And every import in the file must actually resolve.
        import importlib
        importlib.import_module("bybit_connection")

    def test_no_duplicate_top_level_definitions(self):
        import collections

        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "bybit_connection.py",
        )
        tree = ast.parse(open(path, encoding="utf-8").read())
        names = collections.Counter(
            n.name for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        )
        assert {k: v for k, v in names.items() if v > 1} == {}

    def test_place_order_is_keyword_only(self):
        """Positional signatures are how a body ended up in the params slot."""
        import inspect

        sig = inspect.signature(bc.BybitClient.place_order)
        positional = [
            p.name for p in sig.parameters.values()
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
            and p.name != "self"
        ]
        assert positional == []

    def test_request_helper_is_keyword_only_for_params_and_body(self):
        import inspect

        sig = inspect.signature(bc.BybitClient._request)
        assert sig.parameters["params"].kind == inspect.Parameter.KEYWORD_ONLY
        assert sig.parameters["body"].kind == inspect.Parameter.KEYWORD_ONLY

    def test_one_client_class_under_several_names(self):
        assert bc.BillionaireBybitEngine is bc.BybitClient
        assert bc.BybitConnection is bc.BybitClient

    def test_no_perf_counter_used_as_a_clock(self):
        """perf_counter may measure elapsed time; it may not be a timestamp."""
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "bybit_connection.py",
        )
        src = open(path, encoding="utf-8").read()
        offenders = [
            ln.strip() for ln in src.splitlines()
            if "perf_counter" in ln and not ln.lstrip().startswith("#")
            and "``" not in ln
        ]
        assert offenders == []
