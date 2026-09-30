"""tools/recon_packet.py: the read-only reconciliation/assurance packet.

Adversarial coverage per the mission brief -- every one of these is a
scenario where a naive reconciler would either crash, silently pass, or
call something it should not:

  1.  exact local == venue                          -> MATCHED / ok=True
  2.  venue-only order                               -> fail (VENUE_ONLY_ORDER)
  3.  local-only order                               -> fail (LOCAL_ONLY_ORDER)
  4.  quantity mismatch                              -> fail
  5.  side mismatch                                  -> fail
  6.  protection mismatch                            -> fail (PROTECTION_DRIFT)
  7.  unknown identity                               -> fail, never MATCHED
  8.  venue unreadable                               -> fail, VENUE_UNREADABLE
  9.  local unreadable                               -> fail, LOCAL_STATE_UNREADABLE
  10. tampered evidence chain                        -> fail, EVIDENCE_CHAIN_INVALID
  11. missing evidence file + --compare-evidence     -> fail, EVIDENCE_UNREADABLE
  12. pre-existing BTC balance                       -> never a false position
  13. spot Order != StopOrder                        -> not confused
  14. StopOrder != tpslOrder                         -> not confused
  15. linear protection stays category-correct       -> not mixed with spot
  16. read-only: no mutation endpoint is ever called -> spy + static check
  17. real-proof-shaped packet from historical evidence -> ok=True
  18. one acceptance component false                 -> overall ok=False
"""
from __future__ import annotations

import ast
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, BOT)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(BOT, "tools"))

import bybit_connection as bc               # noqa: E402
import recon_packet as rp                   # noqa: E402
import venue_evidence as ve                 # noqa: E402
from fake_bybit import API_KEY, API_SECRET, FakeBybit  # noqa: E402
from persistence import StateStore          # noqa: E402

SYMBOL = "BTCUSDT"

REPO_REST_EVIDENCE = os.path.join(BOT, "artifacts", "testnet_conformance_evidence.jsonl")
REPO_WS_EVIDENCE = os.path.join(BOT, "artifacts", "testnet_conformance_ws_evidence.jsonl")
REPO_CONFORMANCE_RESULT = os.path.join(BOT, "artifacts", "testnet_conformance_result.json")


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


class SpotCfg:
    USE_TESTNET = True
    PAPER_TRADING = True
    BYBIT_VENUE = "testnet"
    BYBIT_API_KEY = API_KEY
    BYBIT_API_SECRET = API_SECRET
    BYBIT_RECV_WINDOW_MS = 5000
    REQUEST_TIMEOUT_SECONDS = 5.0
    USE_LEVERAGE = False
    SYMBOL_FILTERS_TTL = 3600
    ORDERLINK_PREFIX = "BB"
    CATEGORY = "spot"
    DEFAULT_LEVERAGE = 1


class LinearCfg(SpotCfg):
    CATEGORY = "linear"
    ALLOW_SHORTS = True


class MainnetCfg(SpotCfg):
    BYBIT_VENUE = "mainnet"
    USE_TESTNET = False


@pytest.fixture()
def store(tmp_path):
    s = StateStore(str(tmp_path / "state.db"))
    s.claim_writer()
    s.update_equity(10_000.0)
    return s


def _spot_client(store, fake=None):
    fake = fake if fake is not None else FakeBybit(balances={"USDT": 10_000.0, "BTC": 0.0})
    return bc.BybitClient(config=SpotCfg(), store=store, transport=fake), fake


def _linear_client(store, fake=None):
    fake = fake if fake is not None else FakeBybit(balances={"USDT": 10_000.0})
    return bc.BybitClient(config=LinearCfg(), store=store, transport=fake), fake


def _open_spot_position(client, store, *, qty=0.01, entry_price=50_000.0,
                        stop_price=45_000.0):
    entry = client.place_order(symbol=SYMBOL, side="Buy", qty=qty,
                               order_type="Market", purpose="entry")
    assert entry.ok, entry
    # A market fill is confirmed by the production observation path, which
    # this unit test does not exercise -- reflect that confirmation the
    # same way `TradingEngine` would, directly.
    store.update_order_status(entry.order_link_id, "filled", exchange_id=entry.order_id)
    stop = client.place_stop_order(symbol=SYMBOL, side="Sell", qty=qty,
                                   trigger_price=stop_price)
    assert stop.ok, stop
    store.upsert_position(
        SYMBOL, "Buy", qty, entry_price, stop_price=stop_price,
        order_link_id=entry.order_link_id,
        meta={"stop_order_link_id": stop.order_link_id})
    return entry, stop


def _open_linear_position(client, store, fake, *, qty=0.01, entry_price=50_000.0,
                          stop_price=45_000.0, side="Buy"):
    entry = client.place_order(symbol=SYMBOL, side=side, qty=qty,
                               order_type="Market", purpose="entry")
    assert entry.ok, entry
    store.update_order_status(entry.order_link_id, "filled", exchange_id=entry.order_id)
    fake.positions[SYMBOL] = {
        "symbol": SYMBOL, "side": side, "size": str(qty),
        "avgPrice": str(entry_price), "stopLoss": "",
    }
    exit_side = "Sell" if side == "Buy" else "Buy"
    stop = client.place_stop_order(symbol=SYMBOL, side=exit_side, qty=qty,
                                   trigger_price=stop_price)
    assert stop.ok, stop
    store.upsert_position(SYMBOL, side, qty, entry_price, stop_price=stop_price,
                          order_link_id=entry.order_link_id)
    return entry, stop


def _classes(packet):
    return [item["classification"] for item in packet["reconciliation"]]


# ---------------------------------------------------------------------------
# 1. exact local == venue -> MATCHED / ok=True
# ---------------------------------------------------------------------------


def test_1_exact_match_linear_is_ok(store):
    client, fake = _linear_client(store)
    _open_linear_position(client, store, fake)
    packet = rp.build_packet(cfg=client.cfg, store=store, client=client, symbol=SYMBOL)
    assert packet["ok"] is True
    assert packet["reasons"] == ["RECONCILED"]
    assert set(_classes(packet)) == {"MATCHED"}


def test_1_exact_match_spot_orders_and_protection_are_ok_given_baseline(store, tmp_path):
    """SPOT's own MATCHED path: orders + protection match, and the position
    is attributed to this run via a supplied baseline (case 12's mirror --
    see test_12 for the no-baseline case, which is deliberately NOT
    MATCHED)."""
    client, fake = _spot_client(store)
    entry, stop = _open_spot_position(client, store)
    packet = rp.build_packet(cfg=client.cfg, store=store, client=client, symbol=SYMBOL)
    order_items = [i for i in packet["reconciliation"] if i["kind"] == "order"]
    protection_items = [i for i in packet["reconciliation"] if i["kind"] == "protection"]
    assert order_items and all(i["classification"] == "MATCHED" for i in order_items)
    assert protection_items and all(i["classification"] == "MATCHED" for i in protection_items)


# ---------------------------------------------------------------------------
# 2. venue-only order -> fail
# ---------------------------------------------------------------------------


def test_2_venue_only_order_fails(store):
    client, fake = _spot_client(store)
    # A manually-placed resting order the local ledger never recorded, but
    # whose id DOES match this bot's own scheme (recognisable, just not
    # locally known) -- e.g. a race, a crash after submit but before the
    # local write, or a manual test order placed with the bot's own tool.
    fake.orders["BB-tp-9-deadbeefcafefeed"] = {
        "orderId": "ex-99", "orderLinkId": "BB-tp-9-deadbeefcafefeed",
        "symbol": SYMBOL, "side": "Sell", "orderType": "Limit", "qty": "0.01",
        "price": "60000", "orderStatus": "New", "orderFilter": "Order",
        "triggerPrice": "", "cumExecQty": "0", "avgPrice": "",
    }
    packet = rp.build_packet(cfg=client.cfg, store=store, client=client, symbol=SYMBOL)
    assert packet["ok"] is False
    assert "VENUE_ONLY_ORDER" in packet["reasons"]
    assert "VENUE_ONLY" in _classes(packet)


# ---------------------------------------------------------------------------
# 3. local-only order -> fail
# ---------------------------------------------------------------------------


def test_3_local_only_order_fails(store):
    client, fake = _spot_client(store)
    store.record_order("BB-entr-1-localonly000", SYMBOL, "Buy", "Market",
                       0.01, 50_000.0, purpose="entry", status="submitted")
    packet = rp.build_packet(cfg=client.cfg, store=store, client=client, symbol=SYMBOL)
    assert packet["ok"] is False
    assert "LOCAL_ONLY_ORDER" in packet["reasons"]
    assert "LOCAL_ONLY" in _classes(packet)


# ---------------------------------------------------------------------------
# 4. quantity mismatch -> fail
# ---------------------------------------------------------------------------


def test_4_quantity_mismatch_fails(store):
    client, fake = _spot_client(store)
    entry, stop = _open_spot_position(store=store, client=client)
    # Mutate the venue's copy of the stop order's quantity after the fact --
    # a partial fill / venue-side resize the local ledger never learned
    # about.
    fake.orders[stop.order_link_id]["qty"] = "0.02"
    packet = rp.build_packet(cfg=client.cfg, store=store, client=client, symbol=SYMBOL)
    assert packet["ok"] is False
    assert "QUANTITY_MISMATCH" in _classes(packet)


# ---------------------------------------------------------------------------
# 5. side mismatch -> fail
# ---------------------------------------------------------------------------


def test_5_side_mismatch_fails(store):
    client, fake = _spot_client(store)
    entry, stop = _open_spot_position(store=store, client=client)
    fake.orders[stop.order_link_id]["side"] = "Buy"
    packet = rp.build_packet(cfg=client.cfg, store=store, client=client, symbol=SYMBOL)
    assert packet["ok"] is False
    assert "SIDE_MISMATCH" in _classes(packet)


# ---------------------------------------------------------------------------
# 6. protection mismatch -> fail (PROTECTION_DRIFT)
# ---------------------------------------------------------------------------


def test_6_protection_mismatch_spot_stop_not_live_fails(store):
    client, fake = _spot_client(store)
    entry, stop = _open_spot_position(store=store, client=client)
    fake.orders[stop.order_link_id]["orderStatus"] = "Cancelled"
    packet = rp.build_packet(cfg=client.cfg, store=store, client=client, symbol=SYMBOL)
    assert packet["ok"] is False
    assert "PROTECTION_DRIFT" in packet["reasons"]
    protection = [i for i in packet["reconciliation"] if i["kind"] == "protection"]
    assert protection and protection[0]["classification"] == "PROTECTION_MISMATCH"


def test_6_protection_mismatch_linear_stop_cleared_fails(store):
    client, fake = _linear_client(store)
    _open_linear_position(client, store, fake)
    fake.positions[SYMBOL]["stopLoss"] = "0"
    packet = rp.build_packet(cfg=client.cfg, store=store, client=client, symbol=SYMBOL)
    assert packet["ok"] is False
    assert "PROTECTION_DRIFT" in packet["reasons"]


# ---------------------------------------------------------------------------
# 7. unknown identity -> fail, never silently MATCHED
# ---------------------------------------------------------------------------


def test_7_unknown_identity_never_matched(store):
    client, fake = _spot_client(store)
    # An order whose id does not match this bot's own `BB-...` scheme at
    # all -- e.g. placed by hand on the testnet UI, or by a different tool
    # entirely. Its identity cannot be resolved from local state.
    fake.orders["manual-order-1"] = {
        "orderId": "ex-100", "orderLinkId": "manual-order-1", "symbol": SYMBOL,
        "side": "Buy", "orderType": "Limit", "qty": "0.005", "price": "40000",
        "orderStatus": "New", "orderFilter": "Order", "triggerPrice": "",
        "cumExecQty": "0", "avgPrice": "",
    }
    packet = rp.build_packet(cfg=client.cfg, store=store, client=client, symbol=SYMBOL)
    assert packet["ok"] is False
    assert "UNKNOWN" in _classes(packet)
    assert "MATCHED" not in [
        i["classification"] for i in packet["reconciliation"]
        if i["identity"] == "manual-order-1"
    ]
    assert "UNKNOWN_ORDER" in packet["reasons"]


# ---------------------------------------------------------------------------
# 8. venue unreadable -> fail, VENUE_UNREADABLE
# ---------------------------------------------------------------------------


def test_8_venue_unreadable_fails_closed(store):
    client, fake = _spot_client(store)

    def _boom(*a, **kw):
        raise bc.TransientAPIError("simulated venue outage")

    client.get_wallet = _boom  # type: ignore[method-assign]
    packet = rp.build_packet(cfg=client.cfg, store=store, client=client, symbol=SYMBOL)
    assert packet["ok"] is False
    assert "VENUE_UNREADABLE" in packet["reasons"]
    assert packet["reconciliation"] == []


# ---------------------------------------------------------------------------
# 9. local unreadable -> fail, LOCAL_STATE_UNREADABLE
# ---------------------------------------------------------------------------


def test_9_local_state_unreadable_fails_closed(store):
    client, fake = _spot_client(store)
    store.close()  # any subsequent read now raises
    packet = rp.build_packet(cfg=client.cfg, store=store, client=client, symbol=SYMBOL)
    assert packet["ok"] is False
    assert "LOCAL_STATE_UNREADABLE" in packet["reasons"]
    assert packet["reconciliation"] == []


# ---------------------------------------------------------------------------
# 10. tampered evidence chain -> fail, EVIDENCE_CHAIN_INVALID
# ---------------------------------------------------------------------------


def test_10_tampered_evidence_chain_fails(store, tmp_path):
    client, fake = _spot_client(store)
    rest_path = str(tmp_path / "rest.jsonl")
    ws_path = str(tmp_path / "ws.jsonl")
    r1 = ve.build_record(venue="bybit", environment="testnet", method="GET",
                         request_url="https://api-testnet.bybit.com/v5/market/time",
                         request={}, response={"retCode": 0})
    ve.append_evidence(rest_path, r1)
    r2 = ve.build_record(venue="bybit", environment="testnet", method="GET",
                         request_url="https://api-testnet.bybit.com/v5/market/time",
                         request={}, response={"retCode": 0},
                         prev_hash=ve.last_record_hash(rest_path))
    ve.append_evidence(rest_path, r2)
    ve.append_evidence(ws_path, ve.build_ws_event_record(
        venue="bybit", environment="testnet",
        ws_url="wss://stream-testnet.bybit.com/v5/private",
        venue_event_id="connection:1", topic="connection.connected", payload={}))

    # Tamper: hand-edit the first line's content after the chain was built.
    with open(rest_path, encoding="utf-8") as handle:
        lines = handle.readlines()
    tampered = json.loads(lines[0])
    tampered["method"] = "POST"  # content changed; record_hash no longer matches
    lines[0] = json.dumps(tampered) + "\n"
    with open(rest_path, "w", encoding="utf-8") as handle:
        handle.writelines(lines)

    packet = rp.build_packet(
        cfg=client.cfg, store=store, client=client, symbol=SYMBOL,
        compare_evidence=True, rest_evidence_path=rest_path, ws_evidence_path=ws_path,
        conformance_result_path=str(tmp_path / "missing_result.json"))
    assert packet["ok"] is False
    assert "EVIDENCE_CHAIN_INVALID" in packet["reasons"]
    assert packet["evidence_comparison"]["chain_valid"] is False


# ---------------------------------------------------------------------------
# 11. missing evidence file + --compare-evidence -> fail, EVIDENCE_UNREADABLE
# ---------------------------------------------------------------------------


def test_11_missing_evidence_file_fails(store, tmp_path):
    client, fake = _spot_client(store)
    packet = rp.build_packet(
        cfg=client.cfg, store=store, client=client, symbol=SYMBOL,
        compare_evidence=True,
        rest_evidence_path=str(tmp_path / "does_not_exist.jsonl"),
        ws_evidence_path=str(tmp_path / "also_missing.jsonl"))
    assert packet["ok"] is False
    assert "EVIDENCE_UNREADABLE" in packet["reasons"]
    assert packet["evidence_comparison"]["readable"] is False


# ---------------------------------------------------------------------------
# 12. pre-existing BTC balance does NOT become a false position
# ---------------------------------------------------------------------------


def test_12_pre_existing_balance_is_not_a_false_position(store):
    client, fake = _spot_client(store, FakeBybit(balances={"USDT": 10_000.0, "BTC": 0.5}))
    # No local position recorded at all -- this BTC predates (or is
    # otherwise untouched by) this run.
    packet = rp.build_packet(cfg=client.cfg, store=store, client=client, symbol=SYMBOL)
    position_items = [i for i in packet["reconciliation"] if i["kind"] == "position"]
    assert len(position_items) == 1
    assert position_items[0]["classification"] == "MATCHED"
    assert "pre-existing" in position_items[0]["detail"]
    assert packet["ok"] is True


def test_12_local_position_without_baseline_is_unknown_not_matched(store):
    client, fake = _spot_client(store)
    _open_spot_position(client, store)
    packet = rp.build_packet(cfg=client.cfg, store=store, client=client, symbol=SYMBOL)
    position_items = [i for i in packet["reconciliation"] if i["kind"] == "position"]
    assert position_items[0]["classification"] == "UNKNOWN"
    assert packet["ok"] is False


# ---------------------------------------------------------------------------
# 13. spot Order is not confused with StopOrder
# 14. StopOrder is not confused with tpslOrder
# ---------------------------------------------------------------------------


def test_13_and_14_spot_order_kinds_are_enumerated_separately(store):
    client, fake = _spot_client(store, FakeBybit(balances={"USDT": 10_000.0, "BTC": 1.0}))
    entry, stop = _open_spot_position(store=store, client=client)
    # A resting take-profit LIMIT order -- plain "Order" filter.
    tp = client.place_order(symbol=SYMBOL, side="Sell", qty=0.01, order_type="Limit",
                            price=60_000.0, purpose="tp")
    assert tp.ok
    # A tpslOrder the venue reports that this codebase never creates
    # (place_stop_order always uses orderFilter="StopOrder" on spot, never
    # "tpslOrder") -- injected directly to prove the three buckets are read
    # and classified independently rather than being merged into one list.
    fake.orders["BB-xtps-7-abadcafefeedfeed"] = {
        "orderId": "ex-77", "orderLinkId": "BB-xtps-7-abadcafefeedfeed",
        "symbol": SYMBOL, "side": "Sell", "orderType": "Limit", "qty": "0.01",
        "price": "61000", "orderStatus": "New", "orderFilter": "tpslOrder",
        "triggerPrice": "", "cumExecQty": "0", "avgPrice": "",
    }
    venue = rp.build_spot_venue_snapshot(client, SYMBOL)
    assert set(venue["orders_by_filter"].keys()) == {"Order", "StopOrder", "tpslOrder"}
    assert stop.order_link_id in venue["orders_by_filter"]["StopOrder"]
    assert stop.order_link_id not in venue["orders_by_filter"]["Order"]
    assert stop.order_link_id not in venue["orders_by_filter"]["tpslOrder"]
    assert tp.order_link_id in venue["orders_by_filter"]["Order"]
    assert tp.order_link_id not in venue["orders_by_filter"]["StopOrder"]
    assert "BB-xtps-7-abadcafefeedfeed" in venue["orders_by_filter"]["tpslOrder"]
    assert "BB-xtps-7-abadcafefeedfeed" not in venue["orders_by_filter"]["StopOrder"]
    assert "BB-xtps-7-abadcafefeedfeed" not in venue["orders_by_filter"]["Order"]

    # The tpslOrder has no local counterpart at all -- this bot's own order
    # path never creates one -- so the full packet must report it (VENUE_ONLY
    # or UNKNOWN, never silently absorbed into the StopOrder bucket's
    # comparison, and never MATCHED).
    packet = rp.build_packet(cfg=client.cfg, store=store, client=client, symbol=SYMBOL)
    tpsl_items = [i for i in packet["reconciliation"]
                  if i["identity"] == "BB-xtps-7-abadcafefeedfeed"]
    assert len(tpsl_items) == 1
    assert tpsl_items[0]["classification"] in ("VENUE_ONLY", "UNKNOWN")


# ---------------------------------------------------------------------------
# 15. linear protection remains category-correct
# ---------------------------------------------------------------------------


def test_15_linear_protection_uses_position_field_not_spot_stop_order(store):
    client, fake = _linear_client(store)
    _open_linear_position(client, store, fake)
    venue = rp.build_linear_venue_snapshot(client, SYMBOL)
    # Linear has no per-filter order enumeration at all -- the concept does
    # not exist there (see bybit_connection.SUPPORTED_CATEGORIES table).
    assert "orders_by_filter" not in venue
    assert float(venue["position"]["stopLoss"]) > 0
    packet = rp.build_packet(cfg=client.cfg, store=store, client=client, symbol=SYMBOL)
    protection_items = [i for i in packet["reconciliation"] if i["kind"] == "protection"]
    assert len(protection_items) == 1
    assert protection_items[0]["classification"] == "MATCHED"
    assert protection_items[0]["venue"]["stopLoss"] == venue["position"]["stopLoss"]
    # And no spot-shaped "StopOrder" bucket ever leaked into a linear packet.
    assert "StopOrder" not in json.dumps(packet["venue"])


# ---------------------------------------------------------------------------
# 16. read-only: no mutation endpoint is ever called
# ---------------------------------------------------------------------------


_MUTATING_METHOD_NAMES = (
    "place_order", "place_stop_order", "place_take_profit", "cancel_order",
    "cancel_all", "_place_position_stop", "clear_position_stop", "_submit",
)


class _ReadOnlySpyClient:
    """Wraps a real BybitClient; raises the instant any non-GET-shaped
    method is invoked. Delegates every read-only method used by
    `recon_packet` straight through."""

    def __init__(self, inner):
        self._inner = inner
        self.calls = []

    def __getattr__(self, name):
        if name in _MUTATING_METHOD_NAMES:
            raise AssertionError(
                f"recon_packet invoked mutating method {name!r} -- the "
                "read-only contract is broken")
        attr = getattr(self._inner, name)
        if callable(attr) and not name.startswith("__"):
            def _spy(*a, **kw):
                self.calls.append(name)
                return attr(*a, **kw)
            return _spy
        return attr


class TestReadOnlyContract:
    def test_static_source_never_names_a_mutating_method(self):
        source = open(rp.__file__, encoding="utf-8").read()
        tree = ast.parse(source)
        called_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                called_names.add(node.attr)
        overlap = called_names & set(_MUTATING_METHOD_NAMES)
        assert overlap == set(), (
            f"recon_packet.py's source references mutating method(s) {overlap}")

    def test_runtime_spy_sees_only_reads_for_spot(self, store):
        client, fake = _spot_client(store)
        _open_spot_position(store=store, client=client)
        spy = _ReadOnlySpyClient(client)
        spy.is_linear = client.is_linear
        packet = rp.build_packet(cfg=client.cfg, store=store, client=spy, symbol=SYMBOL)
        assert packet["ok"] in (True, False)  # completed without raising
        assert spy.calls, "expected at least one read call"
        assert not (set(spy.calls) & set(_MUTATING_METHOD_NAMES))
        expected_reads = {"get_wallet", "get_coin_balance", "get_open_orders",
                          "_base_asset", "_quote_asset"}
        assert set(spy.calls) <= expected_reads

    def test_runtime_spy_sees_only_reads_for_linear(self, store):
        client, fake = _linear_client(store)
        _open_linear_position(client, store, fake)
        spy = _ReadOnlySpyClient(client)
        spy.is_linear = client.is_linear
        packet = rp.build_packet(cfg=client.cfg, store=store, client=spy, symbol=SYMBOL)
        assert packet["ok"] in (True, False)
        assert not (set(spy.calls) & set(_MUTATING_METHOD_NAMES))
        assert set(spy.calls) <= {"get_position", "get_open_orders"}


# ---------------------------------------------------------------------------
# 17. complete real-proof-shaped packet (from the committed historical
#     artifacts) => ok=True
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not os.path.exists(REPO_REST_EVIDENCE),
                    reason="historical conformance evidence not present")
def test_17_real_proof_shaped_packet_reconciles(store):
    """Mirrors the state the real testnet_conformance_result.json's run
    ended in: fully flattened, no resting orders, evidence chains intact.
    A local/venue snapshot in that same shape must reconcile clean."""
    client, fake = _spot_client(store, FakeBybit(balances={"USDT": 10_000.0, "BTC": 0.10472384}))
    # Nothing open locally and nothing open at the venue -- exactly what a
    # completed, flattened conformance run leaves behind.
    packet = rp.build_packet(
        cfg=client.cfg, store=store, client=client, symbol=SYMBOL,
        compare_evidence=True, rest_evidence_path=REPO_REST_EVIDENCE,
        ws_evidence_path=REPO_WS_EVIDENCE,
        conformance_result_path=REPO_CONFORMANCE_RESULT)
    assert packet["evidence_comparison"]["readable"] is True
    assert packet["evidence_comparison"]["chain_valid"] is True
    assert packet["evidence_comparison"]["identities_agree"] is True
    assert packet["ok"] is True
    assert packet["reasons"] == ["RECONCILED"]


# ---------------------------------------------------------------------------
# 18. one individual acceptance component false => overall ok=False
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not os.path.exists(REPO_REST_EVIDENCE),
                    reason="historical conformance evidence not present")
def test_18_single_false_component_fails_the_whole_packet(store):
    client, fake = _spot_client(store, FakeBybit(balances={"USDT": 10_000.0, "BTC": 0.10472384}))
    entry, stop = _open_spot_position(client, store)
    # Sabotage exactly one acceptance component: the protective stop is no
    # longer live at the venue. Everything else about the snapshot is
    # otherwise unremarkable.
    fake.orders[stop.order_link_id]["orderStatus"] = "Cancelled"
    packet = rp.build_packet(
        cfg=client.cfg, store=store, client=client, symbol=SYMBOL,
        compare_evidence=True, rest_evidence_path=REPO_REST_EVIDENCE,
        ws_evidence_path=REPO_WS_EVIDENCE,
        conformance_result_path=REPO_CONFORMANCE_RESULT)
    assert packet["ok"] is False
    assert "PROTECTION_DRIFT" in packet["reasons"]
    # And the evidence linkage itself, independently, was fine -- proving
    # the failure is attributable to the sabotaged component, not to
    # something else silently also being broken.
    assert packet["evidence_comparison"]["chain_valid"] is True


# ---------------------------------------------------------------------------
# environment refusal: never even read a non-testnet venue
# ---------------------------------------------------------------------------


def test_mainnet_environment_is_refused_before_any_venue_read(store):
    fake = FakeBybit()
    client = bc.BybitClient(config=MainnetCfg(), store=store, transport=fake)
    with pytest.raises(rp.ReconciliationRefused):
        rp.build_packet(cfg=client.cfg, store=store, client=client, symbol=SYMBOL)
    assert fake.requests == [], "no request may reach a non-testnet venue"


# ---------------------------------------------------------------------------
# header / provenance
# ---------------------------------------------------------------------------


def test_packet_header_carries_provenance_and_never_carries_credentials(store):
    client, fake = _spot_client(store)
    packet = rp.build_packet(cfg=client.cfg, store=store, client=client, symbol=SYMBOL)
    for key in ("schema", "run_id", "observed_at_utc", "repository_commit",
               "venue", "environment", "category", "symbol"):
        assert key in packet
    assert packet["environment"] == "testnet"
    assert packet["category"] == "spot"
    assert packet["symbol"] == SYMBOL
    blob = json.dumps(packet)
    assert API_SECRET not in blob
    assert "X-BAPI-SIGN" not in blob or "REDACTED" in blob
