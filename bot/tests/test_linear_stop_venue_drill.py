"""linear_stop_venue_drill.py: the venue evidence Part 1 Item 1 actually
needs — BybitClient.place_stop_order + verify_stop against a real LINEAR
account, never the carry book's own drill.

Two layers, matching the repo's own convention for this exact mechanism
(tests/test_linear_simulator.py::TestTheProtectiveStopEndToEnd):
  - a FakeClient drives the full nine-stage sequence with controllable
    misbehaviour (this is where every fail-closed branch is proven);
  - one test drives `attach`/`verify` against the REAL BybitClient +
    LinearSimulatedExchange, so the drill's stage logic is proven against
    the actual simulated venue mechanics, not only against a hand-rolled
    fake that could quietly drift from what BybitClient really does.
"""
from __future__ import annotations

import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import linear_stop_venue_drill as D  # noqa: E402
from bybit_connection import BybitAPIError, BybitClient, OrderResult  # noqa: E402
from persistence import StateStore  # noqa: E402
from position_sizing import InstrumentFilters  # noqa: E402
from risk_management import BillionaireRiskManager  # noqa: E402
from test_orchestrator import Cfg as _OrchestratorCfg  # noqa: E402
from trading_engine import TradingEngine  # noqa: E402


class _EngineCfg(_OrchestratorCfg):
    """A full config, unlike `_cfg()`'s bare namespace - `BillionaireRiskManager`
    reads MAX_POSITION_SIZE_PCT and friends that the drill's own preflight
    stages never touch."""
    BYBIT_VENUE = "testnet"
    PAPER_TRADING = False
    CATEGORY = "linear"

SYMBOL = "BTCUSDT"
MARK = 100_000.0


def _cfg(**over):
    base = {"BYBIT_VENUE": "testnet", "PAPER_TRADING": False, "CATEGORY": "linear"}
    base.update(over)
    return types.SimpleNamespace(**base)


class FakeClient:
    """A venue that can be made to misbehave on command."""

    def __init__(self, *, mark=MARK, venue_perp=0.0, wallet_ok=True,
                 open_ok=True, attach_ok=True, verify_live=True,
                 flatten_ok=True, clear_ok=True, filters_ok=True,
                 category="linear", venue="testnet", cfg=None,
                 liq_price=None, position_im=None, position_mm=None,
                 leverage=None, wallet_extra=None):
        self.mark = mark
        self.venue_perp = venue_perp
        self.wallet_ok = wallet_ok
        self.open_ok = open_ok
        self.attach_ok = attach_ok
        self.verify_live = verify_live
        self.flatten_ok = flatten_ok
        self.clear_ok = clear_ok
        self.filters_ok = filters_ok
        self.category = category
        self.venue = venue
        self.cfg = cfg if cfg is not None else _cfg(CATEGORY=category)
        self.orders = []
        self.position_size = 0.0
        self.stop_loss = 0.0
        #: Item 4 (--margin-doc) evidence, set only when a test needs it.
        self.liq_price = liq_price
        self.position_im = position_im
        self.position_mm = position_mm
        self.leverage = leverage
        self.wallet_extra = wallet_extra or {}

    @property
    def is_linear(self):
        return self.category == "linear"

    def get_last_price(self, symbol):
        return self.mark

    def get_wallet(self):
        if not self.wallet_ok:
            raise RuntimeError("wallet unreadable")
        return {"accountType": "UNIFIED", **self.wallet_extra}

    def get_position(self, symbol):
        size = self.position_size if self.position_size else self.venue_perp
        if size <= 0:
            return None
        row = {"symbol": symbol, "side": "Buy", "size": str(size),
              "avgPrice": str(self.mark), "markPrice": str(self.mark),
              "stopLoss": str(self.stop_loss),
              "tpslMode": "Full", "slTriggerBy": "LastPrice",
              "slOrderType": "Market", "positionIdx": 0}
        if self.liq_price is not None:
            row["liqPrice"] = str(self.liq_price)
        if self.position_im is not None:
            row["positionIM"] = str(self.position_im)
        if self.position_mm is not None:
            row["positionMM"] = str(self.position_mm)
        if self.leverage is not None:
            row["leverage"] = str(self.leverage)
        return row

    def get_instrument_filters(self, symbol, force=False):
        if not self.filters_ok:
            raise BybitAPIError("filters unreadable")
        return InstrumentFilters(symbol=symbol, tick_size="0.1",
                                 qty_step="0.001", min_qty="0.001",
                                 min_notional="5", from_exchange=True)

    def place_order(self, *, symbol, side, qty, order_type="Market",
                    purpose="", filters=None, reduce_only=False, **kw):
        self.orders.append((side, qty, purpose, reduce_only))
        if reduce_only:
            if not self.flatten_ok:
                return OrderResult(reason="FLATTEN_REJECTED")
            self.position_size = 0.0
            self.stop_loss = 0.0
            return OrderResult(ok=True, order_link_id="flat-1")
        if not self.open_ok:
            return OrderResult(reason="OPEN_REJECTED")
        self.position_size = float(qty)
        return OrderResult(ok=True, order_link_id="open-1")

    def place_stop_order(self, *, symbol, side, qty, trigger_price,
                         filters=None, order_link_id=None):
        if not self.attach_ok:
            return OrderResult(reason="STOP_REJECTED")
        self.stop_loss = trigger_price
        return OrderResult(ok=True, order_link_id="stop-1",
                           raw={"mechanism": "position"})

    def verify_stop(self, *, symbol, order_link_id):
        if self.verify_live is False:
            # Forced failure, for the fail-closed branch - independent of
            # actual stop_loss state.
            return False, "POSITION_HAS_NO_STOP_LOSS"
        live = self.stop_loss > 0
        return live, (f"stopLoss={self.stop_loss}" if live
                      else "POSITION_HAS_NO_STOP_LOSS")

    def clear_position_stop(self, *, symbol):
        if not self.clear_ok:
            return OrderResult(reason="CLEAR_REJECTED")
        self.stop_loss = 0.0
        return OrderResult(ok=True, order_link_id="clear-1",
                           raw={"stopLoss": "0", "mechanism": "position"})


def run(client, **kw):
    kw.setdefault("arm", True)
    kw.setdefault("notional", 100.0)
    return D.run_drill(client=client, **kw)


class TestThePreflightAlonePassesWithoutArming:
    def test_a_dry_run_sends_nothing(self):
        client = FakeClient()
        report = run(client, arm=False)
        assert report["armed"] is False
        assert report["orders_sent"] == 0
        assert client.orders == []
        assert report["verdict"] == "PREFLIGHT_ONLY"

    def test_a_dry_run_reports_reachability_and_auth(self):
        report = run(FakeClient(), arm=False)
        ev = report["evidence"]
        assert ev["reachability"]["last_price"] == MARK
        assert ev["auth"]["auth_ok"] is True
        assert ev["flat"]["venue_perp_qty"] == 0.0


class TestItRefusesBeforeItRisksAnything:
    def test_an_unreachable_venue_stops_at_preflight(self):
        class Blind(FakeClient):
            def get_last_price(self, symbol):
                raise RuntimeError("venue unreachable")
        report = run(Blind(), arm=False)
        assert report["verdict"] == "FAILED"
        assert report["failed_stage"] == "reachability"

    def test_unreadable_wallet_fails_auth(self):
        report = run(FakeClient(wallet_ok=False), arm=False)
        assert report["verdict"] == "FAILED"
        assert report["failed_stage"] == "auth"

    def test_a_position_already_open_stops_it(self):
        report = run(FakeClient(venue_perp=0.5), arm=False)
        assert report["verdict"] == "FAILED"
        assert report["failed_stage"] == "flat"
        assert report["orders_sent"] == 0

    @pytest.mark.parametrize("bad_cfg", [
        _cfg(BYBIT_VENUE="mainnet"),
        _cfg(PAPER_TRADING=True),
        _cfg(CATEGORY="spot"),
    ])
    def test_arming_refuses_off_the_one_safe_configuration(self, bad_cfg):
        client = FakeClient(cfg=bad_cfg)
        with pytest.raises(D.LinearStopDrillRefused):
            D._assert_can_arm(client.cfg)
        # run_drill itself must refuse too, not just the CLI's own check.
        report = run(client)
        assert report["verdict"] == "FAILED"
        assert report["failed_stage"] == "arm_gate"
        assert report["orders_sent"] == 0


class TestTheHappyPathIsAWholeRoundTrip:
    def test_it_opens_attaches_verifies_flattens_and_reconciles(self):
        client = FakeClient()
        report = run(client)
        assert report["verdict"] == "PASSED", report.get("failed_stage")
        assert report["orders_sent"] == 2
        assert report["evidence"]["verify"]["live"] is True
        assert report["evidence"]["final_reconcile"]["verdict"] == "FLAT"
        assert report["evidence"]["final_reconcile"]["stop_still_live"] is False

    def test_position_evidence_dumps_the_real_stop_fields(self):
        report = run(FakeClient())
        ev = report["evidence"]["position_evidence"]
        assert ev["stopLoss"] not in (None, "0", "0.0", "")
        assert ev["tpslMode"] == "Full"


class TestVerifyIsFailClosed:
    def test_a_live_false_verify_fails_the_drill_not_a_silent_pass(self):
        report = run(FakeClient(verify_live=False))
        assert report["verdict"] == "FAILED"
        assert report["failed_stage"] == "verify"
        assert report["evidence"]["verify"]["live"] is False

    def test_the_stop_call_being_rejected_also_fails(self):
        report = run(FakeClient(attach_ok=False))
        assert report["verdict"] == "FAILED"
        assert report["failed_stage"] == "attach"

    def test_the_open_leg_being_rejected_stops_before_any_stop_is_attempted(self):
        client = FakeClient(open_ok=False)
        report = run(client)
        assert report["verdict"] == "FAILED"
        assert report["failed_stage"] == "open"
        assert client.stop_loss == 0.0


class TestAFlattenThatDoesNotLandIsLoud:
    def test_still_open_is_reported_and_a_human_is_told_to_act(self):
        report = run(FakeClient(flatten_ok=False))
        assert report["verdict"] == "FAILED"
        assert report["failed_stage"] == "flatten"
        assert report["still_open"] is True
        assert "HUMAN MUST ACT" in report["next_action"]


class TestItNeverForgesTheGate:
    def test_the_module_never_touches_allows_live_or_the_checklist(self):
        with open(os.path.join(os.path.dirname(__file__), "..", "tools",
                               "linear_stop_venue_drill.py"), encoding="utf-8") as fh:
            body = fh.read()
        for banned in ("allows_live =", "allows_live=True", "\"complete\": True",
                       "clear_kill_switch", "HUMAN_CLEARED_KILL_SWITCH",
                       "promote_forward_shadow"):
            assert banned not in body


class TestLoadClientReadsTheRealEnvironment:
    """The live bug: `config.load({})` treats a non-None mapping as the
    ONLY env source, so `{}` means zero env vars regardless of what the
    shell actually exports - BYBIT_API_SECRET always empty ("cannot sign:
    no API secret configured") and CATEGORY always "spot" even with
    CATEGORY=linear exported. `_load_client` must call `config.load()`
    with no argument (or `None`), which reads the real `os.environ`."""

    def test_load_client_never_calls_load_with_an_empty_mapping(self):
        import ast
        path = os.path.join(os.path.dirname(__file__), "..", "tools",
                            "linear_stop_venue_drill.py")
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=path)
        target = next(n for n in ast.walk(tree)
                      if isinstance(n, ast.FunctionDef) and n.name == "_load_client")
        for node in ast.walk(target):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "load"):
                assert not node.args, (
                    "_load_client must call config.load() with no argument, "
                    "not an empty mapping - {} silently discards os.environ")

    def test_load_client_sees_the_real_environment(self, monkeypatch):
        monkeypatch.setenv("BYBIT_VENUE", "testnet")
        monkeypatch.setenv("PAPER_TRADING", "0")
        monkeypatch.setenv("CATEGORY", "linear")
        monkeypatch.setenv("BYBIT_API_KEY", "k" * 18)
        monkeypatch.setenv("BYBIT_API_SECRET", "s" * 36)

        client, cfg = D._load_client()

        assert cfg.BYBIT_API_SECRET == "s" * 36
        assert cfg.CATEGORY == "linear"
        assert client.category == "linear"
        assert client.is_linear is True
        assert client.api_secret == "s" * 36


# ---------------------------------------------------------------------------
# against the real BybitClient + the real linear simulator - not only a fake
# ---------------------------------------------------------------------------


class TestAgainstTheRealSimulatedVenue:
    """Same pattern as test_linear_simulator.py::TestTheProtectiveStopEndToEnd:
    a real BybitClient talking to LinearSimulatedExchange, so `attach` and
    `verify` are proven against the actual simulated venue mechanics this
    tool's stage functions assume, not only a hand-written fake's promises."""

    @pytest.fixture()
    def client(self):
        import backtest as bt
        view = bt._backtest_config_view(bt.BacktestConfig(category="linear"))
        store = StateStore(":memory:")
        ex = bt.LinearSimulatedExchange(
            {SYMBOL: [bt.Bar(start_ms=1_600_000_000_000, open=100.0,
                             close=100.0, high=100.5, low=99.5, volume=100.0)]
             for _ in [0]}, starting_cash=1_000.0)
        ex.positions[SYMBOL] = bt._SimPosition(
            symbol=SYMBOL, side="Buy", size=1.0, entry_price=100.0)
        yield BybitClient(config=view, store=store, transport=ex), ex
        store.close()

    def test_attach_then_verify_reads_back_live(self, client):
        api, ex = client
        result = api.place_stop_order(
            symbol=SYMBOL, side="Sell", qty=1.0, trigger_price=95.0)
        assert result.ok, result.reason
        live, detail = api.verify_stop(
            symbol=SYMBOL, order_link_id=result.order_link_id)
        assert live is True, detail
        assert ex.positions[SYMBOL].stop_loss == pytest.approx(95.0)

    def test_a_stop_cleared_at_the_venue_reads_back_as_not_live(self, client):
        """The read is against the venue, not local state - same guarantee
        the drill's `verify` stage relies on."""
        api, ex = client
        result = api.place_stop_order(
            symbol=SYMBOL, side="Sell", qty=1.0, trigger_price=95.0)
        ex.positions[SYMBOL].stop_loss = 0.0
        live, detail = api.verify_stop(
            symbol=SYMBOL, order_link_id=result.order_link_id)
        assert live is False
        assert detail == "POSITION_HAS_NO_STOP_LOSS"


# ---------------------------------------------------------------------------
# Item 2 - restart survival: HOLD / VERIFY / FLATTEN
# ---------------------------------------------------------------------------


class TestHoldLeavesThePositionOpen:
    def test_hold_opens_attaches_and_exits_still_open(self):
        client = FakeClient()
        report = D.run_hold(client=client, notional=100.0)
        assert report["verdict"] == "HOLD", report.get("failed_stage")
        assert report["still_open"] is True
        assert report["phase"] == "HOLD"
        assert report["orders_sent"] == 1
        # No flatten stage exists in HOLD - the position must remain on
        # the venue.
        stage_names = [s["stage"] for s in report["stages"]]
        assert "flatten" not in stage_names
        assert client.position_size > 0
        assert client.stop_loss > 0
        assert "POSITION STILL OPEN" in report["next_action"]

    def test_hold_refuses_off_the_one_safe_configuration(self):
        client = FakeClient(cfg=_cfg(PAPER_TRADING=True))
        report = D.run_hold(client=client, notional=100.0)
        assert report["verdict"] == "FAILED"
        assert report["failed_stage"] == "arm_gate"
        assert report["orders_sent"] == 0

    def test_hold_refuses_if_the_venue_already_holds_something(self):
        client = FakeClient(venue_perp=0.5)
        report = D.run_hold(client=client, notional=100.0)
        assert report["verdict"] == "FAILED"
        assert report["failed_stage"] == "flat"
        assert report["orders_sent"] == 0


class TestVerifyReadsTheVenueColdFromANewProcess:
    def test_verify_passes_when_the_stop_is_still_live(self):
        client = FakeClient()
        hold = D.run_hold(client=client, notional=100.0)
        assert hold["verdict"] == "HOLD"
        # A fresh Drill/state - no object shared with run_hold - proves the
        # read is against the venue (FakeClient), not against any leftover
        # in-process state.
        report = D.run_verify(client=client)
        assert report["verdict"] == "VERIFIED", report.get("failed_stage")
        assert report["orders_sent"] == 0
        assert report["evidence"]["verify"]["live"] is True

    def test_verify_fails_closed_if_the_position_is_gone(self):
        client = FakeClient()
        report = D.run_verify(client=client)
        assert report["verdict"] == "FAILED"
        assert report["failed_stage"] == "position_still_open"

    def test_verify_fails_closed_if_the_stop_is_gone_but_the_position_remains(self):
        client = FakeClient()
        client.position_size = 0.001
        client.stop_loss = 0.0
        report = D.run_verify(client=client)
        assert report["verdict"] == "FAILED"
        assert report["failed_stage"] == "verify"
        assert report["evidence"]["verify"]["live"] is False


class TestFlattenClosesOutAfterHoldOrVerify:
    def test_flatten_closes_an_open_position(self):
        client = FakeClient()
        client.position_size = 0.001
        client.stop_loss = 84707.0
        report = D.run_flatten(client=client)
        assert report["verdict"] == "FLATTENED", report.get("failed_stage")
        assert report["evidence"]["final_reconcile"]["verdict"] == "FLAT"
        assert report["evidence"]["final_reconcile"]["stop_still_live"] is False

    def test_flatten_is_a_pass_when_already_flat(self):
        client = FakeClient()
        report = D.run_flatten(client=client)
        assert report["verdict"] == "ALREADY_FLAT"
        assert report["orders_sent"] == 0

    def test_flatten_refuses_off_the_one_safe_configuration(self):
        client = FakeClient(cfg=_cfg(BYBIT_VENUE="mainnet"))
        client.position_size = 0.001
        report = D.run_flatten(client=client)
        assert report["verdict"] == "FAILED"
        assert report["failed_stage"] == "arm_gate"
        assert report["orders_sent"] == 0

    def test_a_flatten_that_does_not_land_is_loud(self):
        client = FakeClient(flatten_ok=False)
        client.position_size = 0.001
        client.stop_loss = 84707.0
        report = D.run_flatten(client=client)
        assert report["verdict"] == "FAILED"
        assert report["still_open"] is True
        assert "HUMAN MUST ACT" in report["next_action"]


class TestItem2NeverCitesSessionTailAsEvidence:
    """The ops-lie this mission exists to fix: LINEAR_STOP_OPS_INVENTORY
    used to point Item 2 at a RUNNABLE `python3 tools/session_tail.py`
    command, which is gate-invariant read-only and never calls verify_stop
    or reads a position's stop fields. Item 2 evidence is only ever a
    HOLD -> VERIFY -> FLATTEN transcript from this tool. Explaining, in
    prose, that session_tail is NOT the evidence is fine and expected -
    what must never reappear is the command telling a human to run it as
    the Item 2 procedure."""

    BANNED = "python3 tools/session_tail.py"

    def test_the_tool_itself_never_shells_out_to_session_tail(self):
        path = os.path.join(os.path.dirname(__file__), "..", "tools",
                            "linear_stop_venue_drill.py")
        with open(path, encoding="utf-8") as fh:
            body = fh.read()
        assert self.BANNED not in body

    def test_docs_no_longer_point_item_2_at_session_tail(self):
        repo_root = os.path.join(os.path.dirname(__file__), "..")
        docs = [
            "docs/promotion/LINEAR_STOP_OPS_INVENTORY.md",
            "artifacts/HUMAN_GATE_OPS_PACKET.md",
            "artifacts/LINEAR_STOP_VENUE_GAP.md",
        ]
        for rel in docs:
            path = os.path.join(repo_root, rel)
            with open(path, encoding="utf-8") as fh:
                body = fh.read()
            assert self.BANNED not in body, (
                f"{rel} still points a human at `{self.BANNED}` as if it "
                "were Item 2 evidence - that tool never calls verify_stop "
                "or reads a position's stop fields")


# ---------------------------------------------------------------------------
# Item 3 - a NAKED position is detected within one cycle: INDUCE / OBSERVE
# ---------------------------------------------------------------------------


def _engine(client, store):
    risk = BillionaireRiskManager(config=client.cfg, store=store)
    return TradingEngine(client=client, risk_manager=risk, store=store,
                         config=client.cfg)


class TestInduceNakedLeavesAnOpenNakedPosition:
    def test_induce_opens_attaches_clears_and_leaves_the_ledger_believing(self):
        client = FakeClient()
        store = StateStore(":memory:")
        report = D.run_induce_naked(client=client, store=store, notional=100.0)
        assert report["verdict"] == "NAKED", report.get("failed_stage")
        assert report["phase"] == "INDUCE"
        assert report["still_open"] is True

        row = {p["symbol"]: p for p in store.open_positions()}.get(SYMBOL)
        assert row is not None
        # The ledger's own stop_price column is untouched by clear_stop -
        # it still believes the stop it wrote during `verify` is live. That
        # gap is exactly what Item 3 exists to catch.
        assert float(row["stop_price"]) > 0

        # The VENUE, independently, shows the stop is gone while the
        # position is still open.
        assert client.position_size > 0
        live, detail = client.verify_stop(symbol=SYMBOL, order_link_id="")
        assert live is False

    def test_induce_refuses_off_the_one_safe_configuration(self):
        client = FakeClient(cfg=_cfg(PAPER_TRADING=True))
        store = StateStore(":memory:")
        report = D.run_induce_naked(client=client, store=store, notional=100.0)
        assert report["verdict"] == "FAILED"
        assert report["failed_stage"] == "arm_gate"

    def test_induce_fails_if_the_venue_never_actually_loses_the_stop(self):
        client = FakeClient(clear_ok=False)
        store = StateStore(":memory:")
        report = D.run_induce_naked(client=client, store=store, notional=100.0)
        assert report["verdict"] == "FAILED"
        assert report["failed_stage"] == "clear_stop"


class TestObserveNakedCallsTheProductionEntrypoint:
    """`run_observe_naked` must call the SAME
    `trading_engine.TradingEngine.check_naked_positions` the live loop's
    `tick()` calls every cycle - not a copy of the attach logic invented
    only for this drill."""

    def test_observe_reprotects_when_the_reattach_succeeds(self):
        client = FakeClient(cfg=_EngineCfg())
        store = StateStore(":memory:")
        induced = D.run_induce_naked(client=client, store=store, notional=100.0)
        assert induced["verdict"] == "NAKED"

        engine = _engine(client, store)
        report = D.run_observe_naked(engine=engine)
        assert report["verdict"] == "REPROTECTED", report.get("failed_stage")
        assert report["phase"] == "OBSERVE"
        result = report["evidence"]["detect_and_act"]["result"]
        assert result["reprotected"] == [SYMBOL]
        live, detail = client.verify_stop(symbol=SYMBOL, order_link_id="")
        assert live is True

    def test_observe_flattens_when_the_reattach_fails(self):
        client = FakeClient(cfg=_EngineCfg())
        store = StateStore(":memory:")
        induced = D.run_induce_naked(client=client, store=store, notional=100.0)
        assert induced["verdict"] == "NAKED"
        # Only NOW does the re-attach start failing - induce's own attach
        # (proving the stop was live before the incident) must succeed.
        client.attach_ok = False

        engine = _engine(client, store)
        report = D.run_observe_naked(engine=engine)
        assert report["verdict"] == "FLATTENED", report.get("failed_stage")
        result = report["evidence"]["detect_and_act"]["result"]
        assert result["flattened"] == [SYMBOL]
        assert client.position_size == 0.0
        assert store.open_position_count() == 0
        # The bracket-failure invariant: emergency close trips the kill
        # switch, the same as any other _emergency_close caller.
        engaged, reason = store.is_kill_switch_engaged()
        assert engaged is True

    def test_observe_fails_closed_if_still_naked_after_the_cycle(self):
        client = FakeClient(cfg=_EngineCfg())
        store = StateStore(":memory:")
        induced = D.run_induce_naked(client=client, store=store, notional=100.0)
        assert induced["verdict"] == "NAKED"
        # Only NOW do filters become unreadable - induce's own venue_rules
        # stage must succeed first.
        client.filters_ok = False

        engine = _engine(client, store)
        report = D.run_observe_naked(engine=engine)
        assert report["verdict"] == "FAILED"
        assert report["failed_stage"] == "confirm_outcome"
        # Still naked: never silently reported as resolved.
        live, detail = client.verify_stop(symbol=SYMBOL, order_link_id="")
        assert live is False
        assert client.position_size > 0

    def test_observe_refuses_without_the_ledger_row_induce_wrote(self):
        client = FakeClient(cfg=_EngineCfg())
        store = StateStore(":memory:")  # never induced - empty ledger
        engine = _engine(client, store)
        report = D.run_observe_naked(engine=engine)
        assert report["verdict"] == "FAILED"
        assert report["failed_stage"] == "position_known_to_ledger"

    def test_observe_records_wall_clock_timestamps(self):
        client = FakeClient(cfg=_EngineCfg())
        store = StateStore(":memory:")
        induced = D.run_induce_naked(client=client, store=store, notional=100.0)
        engine = _engine(client, store)
        report = D.run_observe_naked(engine=engine,
                                     induce_finished_ms=induced["finished_ms"])
        ts = report["timestamps_ms"]
        assert ts["induce_finished_ms"] == induced["finished_ms"]
        assert (ts["observe_started_ms"] <= ts["action_started_ms"]
               <= ts["action_finished_ms"] <= ts["observe_finished_ms"])


class TestClearPositionStopOnBybitClient:
    """The real client method induce-naked uses to lose the stop AT THE
    VENUE - added once on BybitClient, per the "one dialect" rule, rather
    than hand-rolled in the drill."""

    def test_spot_refuses_it_has_no_position_stop_to_clear(self):
        import backtest as bt
        from persistence import StateStore as RealStateStore

        view = bt._backtest_config_view(bt.BacktestConfig(category="spot"))
        store = RealStateStore(":memory:")
        client = BybitClient(config=view, store=store)
        result = client.clear_position_stop(symbol=SYMBOL)
        assert not result.ok
        assert result.reason == "SPOT_HAS_NO_POSITION_STOP_TO_CLEAR"
        store.close()

    def test_linear_clears_the_stop_against_the_real_simulator(self):
        import backtest as bt

        view = bt._backtest_config_view(bt.BacktestConfig(category="linear"))
        store = StateStore(":memory:")
        ex = bt.LinearSimulatedExchange(
            {SYMBOL: [bt.Bar(start_ms=1_600_000_000_000, open=100.0,
                             close=100.0, high=100.5, low=99.5, volume=100.0)]
             for _ in [0]}, starting_cash=1_000.0)
        ex.positions[SYMBOL] = bt._SimPosition(
            symbol=SYMBOL, side="Buy", size=1.0, entry_price=100.0)
        client = BybitClient(config=view, store=store, transport=ex)

        placed = client.place_stop_order(
            symbol=SYMBOL, side="Sell", qty=1.0, trigger_price=95.0)
        assert placed.ok
        live, _ = client.verify_stop(symbol=SYMBOL, order_link_id="")
        assert live is True

        result = client.clear_position_stop(symbol=SYMBOL)
        assert result.ok, result.reason
        live, detail = client.verify_stop(symbol=SYMBOL, order_link_id="")
        assert live is False
        assert detail == "POSITION_HAS_NO_STOP_LOSS"
        store.close()


class TestItem3NeverCitesFalseEvidence:
    """Item 3's docs must never resurrect either of the false pointers this
    project already killed: the carry drill (which never calls
    place_stop_order/verify_stop at all) and session_tail.py (read-only,
    gate-invariant, never touches a position's stop)."""

    BANNED_SESSION_TAIL = "python3 tools/session_tail.py"

    def test_item_3_docs_do_not_point_at_session_tail(self):
        repo_root = os.path.join(os.path.dirname(__file__), "..")
        docs = [
            "docs/promotion/LINEAR_STOP_OPS_INVENTORY.md",
            "artifacts/HUMAN_GATE_OPS_PACKET.md",
            "artifacts/LINEAR_STOP_VENUE_GAP.md",
        ]
        for rel in docs:
            path = os.path.join(repo_root, rel)
            with open(path, encoding="utf-8") as fh:
                body = fh.read()
            assert self.BANNED_SESSION_TAIL not in body, (
                f"{rel} points Item 3 at session_tail.py - that tool never "
                "calls verify_stop or reads a position's stop fields")


# ---------------------------------------------------------------------------
# Item 4 - margin and liquidation at the proposed notional, documented.
# ---------------------------------------------------------------------------


class TestMarginDocDumpsVenueTruth:
    def test_documents_a_full_dump_when_the_venue_has_the_fields(self):
        client = FakeClient(liq_price=70_000.0, position_im=5.0,
                           position_mm=1.0, leverage=10,
                           wallet_extra={"totalEquity": "9999",
                                        "totalAvailableBalance": "8888",
                                        "coin": [
                                            {"coin": "USDT", "walletBalance": "9999",
                                             "equity": "9999",
                                             "availableToWithdraw": "8888",
                                             "usdValue": "9999"},
                                            {"coin": "BTC", "walletBalance": "0.01",
                                             "equity": "0.01", "usdValue": "1000",
                                             "collateralSwitch": "ON"}]})
        report = D.run_margin_doc(client=client, notional=100.0)
        assert report["verdict"] == "DOCUMENTED", report.get("failed_stage")
        assert report["phase"] == "MARGIN_DOC"

        pos = report["evidence"]["dump"]["position"]
        assert pos["positionIM"] == "5.0"
        assert pos["positionMM"] == "1.0"
        assert pos["leverage"] == "10"
        assert pos["liq_price"]["value"] == 70_000.0
        assert pos["liq_price"]["key_found"] == "liqPrice"

        wallet = report["evidence"]["dump"]["wallet"]
        assert wallet["totalEquity"] == "9999"
        assert wallet["coin"]["USDT"]["walletBalance"] == "9999"
        assert wallet["coin"]["BTC"]["collateralSwitch"] == "ON"

    def test_absent_fields_are_recorded_explicitly_never_invented(self):
        client = FakeClient()  # no liq/im/mm/wallet_extra set
        report = D.run_margin_doc(client=client, notional=100.0)
        assert report["verdict"] == "DOCUMENTED"
        pos_evidence = report["evidence"]["dump"]
        assert "positionIM" in pos_evidence["position_absent_keys"]
        assert "positionMM" in pos_evidence["position_absent_keys"]
        assert pos_evidence["position"]["positionIM"] is None
        assert pos_evidence["position"]["liq_price"]["reason"] == "ABSENT"
        assert pos_evidence["position"]["liq_price"]["key_found"] is None
        wallet_evidence = pos_evidence["wallet"]
        assert wallet_evidence["totalEquity"] is None
        assert "totalEquity" in pos_evidence["wallet_absent_keys"]
        assert "USDT.walletBalance" in pos_evidence["wallet_absent_keys"]


class TestMarginDocDerivesNearerAndAtCap:
    def test_stop_nearer_than_liquidation(self):
        # stop at 5% below mark (~95000); liq far below that.
        client = FakeClient(liq_price=50_000.0)
        report = D.run_margin_doc(client=client, notional=100.0)
        derived = report["evidence"]["derive"]
        assert derived["nearer"] == "stop"
        assert "should fire first" in derived["risk_line"]

    def test_liquidation_nearer_than_stop_is_named_loudly(self):
        # liq just above the stop price (closer to entry than the stop is).
        client = FakeClient(liq_price=99_000.0)
        report = D.run_margin_doc(client=client, notional=100.0)
        derived = report["evidence"]["derive"]
        assert derived["nearer"] == "liq"
        assert "LIQUIDATION IS NEARER" in derived["risk_line"]

    def test_nearer_is_unknown_without_a_liq_price(self):
        client = FakeClient()  # no liq_price
        report = D.run_margin_doc(client=client, notional=100.0)
        derived = report["evidence"]["derive"]
        assert derived["nearer"] == "unknown"
        assert derived["liq_price"] is None
        assert "UNKNOWN" in derived["risk_line"]

    def test_at_cap_is_measured_against_the_real_shadow_cap(self):
        import shadow
        client = FakeClient()
        report = D.run_margin_doc(client=client, notional=100.0)
        at_cap = report["evidence"]["derive"]["at_cap"]
        assert at_cap["cap_usd"] == float(shadow.SHADOW_MAX_NOTIONAL_USD)
        assert at_cap["notional_usd"] == pytest.approx(
            client.position_size * client.mark)


class TestMarginDocNeverAutoFlattens:
    def test_the_position_is_left_open_for_a_human_to_flatten(self):
        client = FakeClient()
        report = D.run_margin_doc(client=client, notional=100.0)
        assert report["still_open"] is True
        assert client.position_size > 0
        assert "RUN --flatten NEXT" in report["next_action"]
        stage_names = [s["stage"] for s in report["stages"]]
        assert "flatten" not in stage_names

    def test_flatten_afterward_closes_it(self):
        client = FakeClient()
        induced = D.run_margin_doc(client=client, notional=100.0)
        assert induced["verdict"] == "DOCUMENTED"
        flattened = D.run_flatten(client=client)
        assert flattened["verdict"] == "FLATTENED", flattened.get("failed_stage")


class TestMarginDocRefusesBeforeItRisksAnything:
    def test_refuses_off_the_one_safe_configuration(self):
        client = FakeClient(cfg=_cfg(PAPER_TRADING=True))
        report = D.run_margin_doc(client=client, notional=100.0)
        assert report["verdict"] == "FAILED"
        assert report["failed_stage"] == "arm_gate"
        assert report["orders_sent"] == 0

    def test_refuses_if_the_venue_already_holds_something(self):
        client = FakeClient(venue_perp=0.5)
        report = D.run_margin_doc(client=client, notional=100.0)
        assert report["verdict"] == "FAILED"
        assert report["failed_stage"] == "flat"
        assert report["orders_sent"] == 0


class TestFindLiqPriceHandlesTheBeyondBoundsCase:
    """Matches CarryBroker.get_liquidation_view's proven-live handling: an
    empty string means "beyond the venue's bounds", not zero."""

    def test_empty_string_is_beyond_bounds_not_zero(self):
        info = D._find_liq_price({"liqPrice": ""})
        assert info["value"] is None
        assert info["reason"] == "BEYOND_VENUE_PRICE_BOUNDS_OR_EMPTY"

    def test_a_real_value_is_parsed(self):
        info = D._find_liq_price({"liqPrice": "70000.5"})
        assert info["value"] == 70_000.5
        assert info["key_found"] == "liqPrice"

    def test_the_rename_alias_is_also_searched(self):
        info = D._find_liq_price({"liquidationPrice": "65000"})
        assert info["value"] == 65_000.0
        assert info["key_found"] == "liquidationPrice"

    def test_absent_from_both_aliases_is_reported_as_absent(self):
        info = D._find_liq_price({})
        assert info["key_found"] is None
        assert info["reason"] == "ABSENT"
        assert info["aliases_searched"] == list(D.LIQ_PRICE_ALIASES)


class TestMarginDocAgainstTheRealSimulatedVenue:
    """The dump helpers, proven against a real BybitClient position row from
    the linear simulator - not only a hand-rolled fake."""

    def test_dump_reads_real_simulator_fields_and_reports_the_rest_absent(self):
        import backtest as bt

        view = bt._backtest_config_view(bt.BacktestConfig(category="linear"))
        store = StateStore(":memory:")
        ex = bt.LinearSimulatedExchange(
            {SYMBOL: [bt.Bar(start_ms=1_600_000_000_000, open=100.0,
                             close=100.0, high=100.5, low=99.5, volume=100.0)]
             for _ in [0]}, starting_cash=1_000.0)
        ex.positions[SYMBOL] = bt._SimPosition(
            symbol=SYMBOL, side="Buy", size=1.0, entry_price=100.0,
            stop_loss=95.0)
        client = BybitClient(config=view, store=store, transport=ex)

        row = client.get_position(SYMBOL)
        assert row is not None
        dumped = D._dump_position_fields(row)
        # The simulator DOES model these:
        assert dumped["fields"]["markPrice"] is not None
        assert dumped["fields"]["stopLoss"] is not None
        assert dumped["fields"]["leverage"] is not None
        # The simulator does NOT model IM/MM - absent, never invented:
        assert dumped["fields"]["positionIM"] is None
        assert "positionIM" in dumped["absent_keys"]
        assert dumped["fields"]["positionMM"] is None

        wallet = client.get_wallet()
        wallet_dumped = D._dump_wallet_fields(wallet)
        assert isinstance(wallet_dumped["fields"], dict)
        store.close()


class TestItem4NeverCitesFalseEvidence:
    """Item 4's docs must give the real --margin-doc command, never point at
    the carry drill or session_tail.py, and must not claim the simulator's
    liquidation formula stands in for venue truth."""

    def test_item_4_docs_do_not_point_at_session_tail(self):
        repo_root = os.path.join(os.path.dirname(__file__), "..")
        docs = [
            "docs/promotion/LINEAR_STOP_OPS_INVENTORY.md",
            "artifacts/HUMAN_GATE_OPS_PACKET.md",
            "artifacts/LINEAR_STOP_VENUE_GAP.md",
        ]
        for rel in docs:
            path = os.path.join(repo_root, rel)
            with open(path, encoding="utf-8") as fh:
                body = fh.read()
            assert "python3 tools/session_tail.py" not in body

    def test_the_margin_memo_disclaims_the_simulator_formula(self):
        repo_root = os.path.join(os.path.dirname(__file__), "..")
        path = os.path.join(repo_root, "docs", "promotion",
                            "LINEAR_STOP_MARGIN_MEMO.md")
        with open(path, encoding="utf-8") as fh:
            body = fh.read()
        assert "NOT a substitute" in body or "NOT evidence" in body

    def test_the_checklist_item_4_observed_line_is_still_blank(self):
        repo_root = os.path.join(os.path.dirname(__file__), "..")
        path = os.path.join(repo_root, "docs", "promotion",
                            "LINEAR_STOP_VERIFICATION_CHECKLIST.md")
        with open(path, encoding="utf-8") as fh:
            body = fh.read()
        assert "[ ] 4. Margin and liquidation behaviour" in body
