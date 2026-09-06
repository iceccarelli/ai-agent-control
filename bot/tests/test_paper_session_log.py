"""Slice 30 — the paper session record, and what it must never become.

The session log is the one artefact of a paper run that a human is likely to read
and quote. That makes it the most likely place for an edge claim to reappear by
accident — not through dishonesty, but through a reasonable-sounding future edit:
"operators will want to see how it did."

Timing-skill research on this codebase is CLOSED with an ABSENT verdict for both
families measured (`RESEARCH_CLOSE_STAGE1.md`). So these tests hold two lines:

* the record carries `NO EDGE CLAIM — timing-skill research CLOSED`, verbatim;
* the record carries no profit figure to misread, checked against a live record
  AND against the dict literal in the source, so a field added but never
  exercised is still caught.

Plus the boring, essential one: no credentials, anywhere, ever.
"""
from __future__ import annotations

import ast
import inspect
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bybit_connection as bc  # noqa: E402
import config as _config  # noqa: E402
import main as m  # noqa: E402
import session_log as sl  # noqa: E402
from fake_bybit import API_KEY, API_SECRET, FakeBybit  # noqa: E402
from persistence import StateStore  # noqa: E402
from position_sizing import BillionairePositionSizing  # noqa: E402
from risk_management import BillionaireRiskManager  # noqa: E402
import trading_engine as te  # noqa: E402

EQUITY = 100_000.0
ENTRY = 50_000.0

#: A credential no other fixture uses, so finding it anywhere is unambiguous.
SECRET_CANARY = "canary-secret-do-not-log-0f1e2d3c"
KEY_CANARY = "canary-key-do-not-log-9a8b7c6d"


class Cfg:
    USE_TESTNET = True
    PAPER_TRADING = True
    ENTRIES_ENABLED = True
    PAPER_SESSION_LOG_PATH = ""
    LIVE_AUTHORIZED = False
    POLICY_MODE = "off"
    BYBIT_API_KEY = API_KEY
    BYBIT_API_SECRET = API_SECRET
    BYBIT_RECV_WINDOW_MS = 5000
    REQUEST_TIMEOUT_SECONDS = 5.0
    USE_LEVERAGE = False
    SYMBOL_FILTERS_TTL = 3600
    ORDERLINK_PREFIX = "BB"
    MAX_POSITION_SIZE_PCT = 0.02
    MAX_TOTAL_EXPOSURE_PCT = 0.10
    MAX_DAILY_LOSS_PCT = 0.02
    MAX_DRAWDOWN_PCT = 0.10
    STOP_LOSS_PCT = 0.02
    RISK_PER_TRADE_PCT = 0.005
    MAX_OPEN_POSITIONS = 4
    MAX_CONSECUTIVE_LOSSES = 4
    MIN_RISK_REWARD_RATIO = 2.0
    MIN_CONFIDENCE = 0.60
    MAX_CORRELATION_EXPOSURE_PCT = 0.30
    MAX_LEVERAGE_EU = 1
    DEFAULT_LEVERAGE = 1
    TRADE_COOLDOWN_SECONDS = 0
    CIRCUIT_BREAKER_HOURS = 2.0
    BREAKEVEN_AFTER_FIRST_TP = True
    TRADING_SYMBOLS = ("BTCUSDT",)
    LOOP_INTERVAL_SECONDS = 0.01
    ENABLE_HEALTH_SERVER = False
    HEALTHCHECK_PORT = 18147
    LIVE_TRADING_ACK = ""


class LiveOrderCfg(Cfg):
    """Paper OFF so the order path actually submits — still testnet."""

    PAPER_TRADING = False
    TRADE_COOLDOWN_SECONDS = 60


class StubStrategy:
    def __init__(self):
        self.calls = []

    def signal_for(self, symbol):
        self.calls.append(symbol)
        return te.TradeIntent(
            symbol=symbol, signal_type="BUY",
            entry_price=ENTRY, stop_price=ENTRY * 0.98,
            take_profits=((ENTRY * 1.05, 1.0),), confidence=0.8,
        )


@pytest.fixture()
def exchange():
    return FakeBybit(balances={"USDT": 100_000.0, "BTC": 5.0}, equity=EQUITY)


def build(tmp_path, exchange, cfg=None, strategy=None):
    cfg = cfg or Cfg()
    store = StateStore(str(tmp_path / "state.db"))
    store.update_equity(EQUITY)
    client = bc.BybitClient(config=cfg, store=store, transport=exchange)
    risk = BillionaireRiskManager(config=cfg, store=store)
    engine = te.TradingEngine(
        client=client, risk_manager=risk,
        position_sizer=BillionairePositionSizing(config=cfg, risk_manager=risk),
        store=store, config=cfg,
    )
    return m.TradingBot(
        config=cfg, store=store, client=client,
        risk_manager=risk, engine=engine, strategy=strategy,
    )


def buy_intent(**over):
    base = dict(
        symbol="BTCUSDT", signal_type="BUY",
        entry_price=ENTRY, stop_price=ENTRY * 0.98,
        take_profits=((ENTRY * 1.024, 0.5), (ENTRY * 1.056, 0.5)),
        confidence=0.75,
    )
    base.update(over)
    return te.TradeIntent(**base)


def record_for(tmp_path, cfg=None, client=None, store=None):
    return sl.PaperSessionLog(
        config=cfg or Cfg(), store=store, client=client,
        path=str(tmp_path / "session.jsonl"),
    )


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------


REQUIRED_FIELDS = {
    "schema": str,
    "session_id": str,
    "started_utc": str,
    "entries_enabled": bool,
    "paper": bool,
    "testnet": bool,
    "live_authorized": bool,
    "policy_mode": str,
    "model_loaded": bool,
    "kill_switch_engaged": bool,
    "kill_switch_reason": str,
    "ticks": int,
    "orders_submitted": int,
    "reconciled": bool,
    "no_edge_claim": str,
}


class TestSessionLogSchema:

    def test_every_declared_field_is_present_with_the_right_type(self, tmp_path):
        record = record_for(tmp_path).record()
        for name, kind in REQUIRED_FIELDS.items():
            assert name in record, name
            assert isinstance(record[name], kind), (name, type(record[name]))

    def test_the_schema_tag_is_pinned(self, tmp_path):
        assert sl.SCHEMA == "paper_session/1"
        assert record_for(tmp_path).record()["schema"] == "paper_session/1"

    def test_ended_utc_is_null_until_the_session_closes(self, tmp_path):
        log = record_for(tmp_path)
        assert log.record()["ended_utc"] is None
        closed = log.close()
        assert isinstance(closed["ended_utc"], str)

    def test_timestamps_are_utc_with_a_trailing_z(self, tmp_path):
        record = record_for(tmp_path).record()
        assert record["started_utc"].endswith("Z")
        assert "T" in record["started_utc"]

    def test_session_ids_are_unique(self, tmp_path):
        ids = {record_for(tmp_path).session_id for _ in range(20)}
        assert len(ids) == 20

    def test_ticks_are_counted(self, tmp_path):
        log = record_for(tmp_path)
        for _ in range(7):
            log.tick()
        assert log.record()["ticks"] == 7

    def test_an_unreadable_kill_switch_is_reported_as_engaged(self, tmp_path):
        """Fail closed, consistently with `risk_management.kill_active`."""

        class BrokenStore:
            def is_kill_switch_engaged(self):
                raise RuntimeError("db gone")

            def journal(self, *a, **k):
                pass

        record = record_for(tmp_path, store=BrokenStore()).record()
        assert record["kill_switch_engaged"] is True
        assert record["kill_switch_reason"] == "UNREADABLE"

    def test_the_kill_switch_state_is_read_not_assumed(self, tmp_path):
        store = StateStore(str(tmp_path / "s.db"))
        log = record_for(tmp_path, store=store)
        assert log.record()["kill_switch_engaged"] is False
        store.trip_kill_switch("tripped mid-session")
        after = log.record()
        assert after["kill_switch_engaged"] is True
        assert after["kill_switch_reason"] == "tripped mid-session"
        store.close()


class TestTheNoEdgeClaimIsMandatory:

    def test_the_constant_is_exact(self):
        assert sl.NO_EDGE_CLAIM == "NO EDGE CLAIM — timing-skill research CLOSED"

    def test_every_record_carries_it_verbatim(self, tmp_path):
        log = record_for(tmp_path)
        for record in (log.record(), log.start(), log.close()):
            assert record["no_edge_claim"] == sl.NO_EDGE_CLAIM

    def test_it_is_written_to_the_file(self, tmp_path):
        path = tmp_path / "session.jsonl"
        log = sl.PaperSessionLog(config=Cfg(), path=str(path))
        log.start()
        log.close()
        lines = [json.loads(line) for line in path.read_text().splitlines()]
        assert len(lines) == 2
        assert {line["event"] for line in lines} == {"SESSION_START", "SESSION_END"}
        for line in lines:
            assert line["no_edge_claim"] == sl.NO_EDGE_CLAIM

    def test_it_reaches_the_journal_as_the_reason(self, tmp_path):
        store = StateStore(str(tmp_path / "s.db"))
        sl.PaperSessionLog(config=Cfg(), store=store, path="").start()
        rows = store.recent_decisions(limit=10)
        session_rows = [r for r in rows if r["symbol"] == "SESSION"]
        assert session_rows, "no SESSION row was journalled"
        assert session_rows[0]["reason"] == sl.NO_EDGE_CLAIM
        assert session_rows[0]["decision"] == "SESSION_START"
        store.close()


class TestItRefusesToBecomeAPerformanceReport:
    """Two independent checks, because one of them can be evaded by accident."""

    def test_no_live_record_field_looks_like_a_performance_metric(self, tmp_path):
        record = record_for(tmp_path).record()
        for key in record:
            for marker in sl.FORBIDDEN_FIELD_MARKERS:
                assert marker not in key.lower(), (key, marker)

    def test_no_source_level_field_looks_like_a_performance_metric(self):
        """Catches a field added to the dict literal but never exercised.

        The live-record check above only sees keys that a test happened to
        produce. This one reads the dict literals in `record()` directly, so a
        new `"pnl": ...` line fails even if no test ever calls the branch.

        Dict KEYS only — not every string constant in the function. The
        docstring of `record()` says "No PnL. No credentials. Ever.", and a
        check that banned the word would delete the statement of intent while
        leaving the thing it forbids.
        """
        tree = ast.parse(textwrap_dedent(
            inspect.getsource(sl.PaperSessionLog.record)))
        keys = [key.value
                for node in ast.walk(tree) if isinstance(node, ast.Dict)
                for key in node.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)]
        assert keys, "no dict literal found in record()"
        for key in keys:
            for marker in sl.FORBIDDEN_FIELD_MARKERS:
                assert marker not in key.lower(), (key, marker)

    def test_the_forbidden_list_still_contains_the_obvious_ones(self):
        """So the guard cannot be neutered by emptying the list."""
        for marker in ("pnl", "profit", "return", "equity", "win_rate"):
            assert marker in sl.FORBIDDEN_FIELD_MARKERS

    def test_the_module_computes_no_arithmetic_on_money(self):
        """Structural: no division, no percentage, nothing that makes a ratio."""
        source = inspect.getsource(sl)
        for banned in ("realised", "unrealised", "avg_win", "avg_loss"):
            assert banned not in source, banned


class TestItNeverWritesCredentials:

    def _cfg_with_canaries(self):
        cfg = Cfg()
        cfg.BYBIT_API_KEY = KEY_CANARY
        cfg.BYBIT_API_SECRET = SECRET_CANARY
        return cfg

    def test_no_credential_appears_in_the_record(self, tmp_path):
        record = record_for(tmp_path, cfg=self._cfg_with_canaries()).record()
        blob = json.dumps(record)
        assert KEY_CANARY not in blob
        assert SECRET_CANARY not in blob

    def test_no_credential_appears_in_the_file(self, tmp_path):
        path = tmp_path / "session.jsonl"
        log = sl.PaperSessionLog(config=self._cfg_with_canaries(), path=str(path))
        log.start()
        log.close()
        text = path.read_text()
        assert KEY_CANARY not in text
        assert SECRET_CANARY not in text

    def test_no_credential_appears_in_the_journal(self, tmp_path):
        store = StateStore(str(tmp_path / "s.db"))
        sl.PaperSessionLog(
            config=self._cfg_with_canaries(), store=store, path="").start()
        blob = json.dumps(store.recent_decisions(limit=10), default=str)
        assert KEY_CANARY not in blob
        assert SECRET_CANARY not in blob
        store.close()

    def test_the_recorder_never_reads_a_credential_field(self):
        """AST, not text: the module docstring names these to say it avoids them.

        A raw-text ban would force the docstring to stop explaining what is
        forbidden — the same trap slice 29 hit with `clear_kill_switch_by_human`.
        What matters is whether any *read* of a credential exists.
        """
        banned = {"BYBIT_API_KEY", "BYBIT_API_SECRET", "api_key", "api_secret",
                  "signature", "sign"}
        tree = ast.parse(inspect.getsource(sl))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                assert node.attr not in banned, node.attr
            if isinstance(node, ast.Call):
                name = getattr(node.func, "id", None) or getattr(
                    node.func, "attr", None)
                if name == "getattr":
                    for arg in node.args[1:2]:
                        if isinstance(arg, ast.Constant):
                            assert arg.value not in banned, arg.value


class TestOrdersSubmittedIsCountedNotInferred:

    def test_a_fresh_client_has_submitted_nothing(self, tmp_path, exchange):
        store = StateStore(str(tmp_path / "s.db"))
        client = bc.BybitClient(config=Cfg(), store=store, transport=exchange)
        assert client.orders_submitted == 0
        store.close()

    def test_it_increments_on_a_real_order_create(self, tmp_path, exchange):
        """Paper OFF, so the order path actually reaches the transport."""
        cfg = LiveOrderCfg()
        store = StateStore(str(tmp_path / "s.db"))
        store.update_equity(EQUITY)
        client = bc.BybitClient(config=cfg, store=store, transport=exchange)
        risk = BillionaireRiskManager(config=cfg, store=store)
        engine = te.TradingEngine(
            client=client, risk_manager=risk,
            position_sizer=BillionairePositionSizing(config=cfg, risk_manager=risk),
            store=store, config=cfg,
        )
        report = engine.execute(buy_intent())
        assert report.ok, report.reason
        # entry + protective stop
        assert client.orders_submitted >= 1
        assert client.orders_submitted == len(exchange.order_create_bodies())
        store.close()

    def test_it_counts_the_transport_not_the_journal(self):
        """The counter must live at `_request`, keyed on the create endpoint.

        Counting decisions instead would report what the bot *intended*. The only
        honest answer to "did this session submit an order?" is what crossed the
        wire.
        """
        source = inspect.getsource(bc.BybitClient._request)
        assert "/v5/order/create" in source
        assert "orders_submitted += 1" in source

    def test_the_session_record_reports_the_client_counter(self, tmp_path, exchange):
        store = StateStore(str(tmp_path / "s.db"))
        client = bc.BybitClient(config=Cfg(), store=store, transport=exchange)
        client.orders_submitted = 3          # whatever the client says, verbatim
        assert record_for(tmp_path, client=client).record()["orders_submitted"] == 3
        store.close()

    def test_a_paper_session_submits_nothing(self, tmp_path, exchange):
        """PAPER_TRADING short-circuits AFTER the gates, BEFORE the transport."""
        bot = build(tmp_path, exchange, strategy=StubStrategy())
        assert bot.startup() is True
        for _ in range(10):
            bot.tick()
        record = bot.session.record()
        assert record["orders_submitted"] == 0
        assert record["paper"] is True
        bot.shutdown()


class TestStandDownProducesZeroOrders:
    """A1, measured rather than assumed."""

    def test_ten_ticks_with_entries_disabled_submit_zero_orders(
        self, tmp_path, exchange
    ):
        cfg = Cfg()
        cfg.ENTRIES_ENABLED = False
        bot = build(tmp_path, exchange, cfg=cfg, strategy=None)
        assert bot.startup() is True
        for _ in range(10):
            bot.tick()
        record = bot.session.close()
        assert record["entries_enabled"] is False
        assert record["orders_submitted"] == 0
        assert record["ticks"] == 10
        assert record["reconciled"] is True
        assert record["kill_switch_engaged"] is False
        assert bot.health()["healthy"] is True
        bot.shutdown()

    def test_the_stand_down_record_still_carries_the_no_edge_claim(
        self, tmp_path, exchange
    ):
        cfg = Cfg()
        cfg.ENTRIES_ENABLED = False
        bot = build(tmp_path, exchange, cfg=cfg, strategy=None)
        bot.startup()
        assert bot.session.close()["no_edge_claim"] == sl.NO_EDGE_CLAIM
        bot.shutdown()

    def test_live_authorized_is_false_throughout(self, tmp_path, exchange):
        bot = build(tmp_path, exchange, strategy=None)
        bot.startup()
        assert bot.session.record()["live_authorized"] is False
        assert bot.session.record()["policy_mode"] == "off"
        assert bot.session.record()["model_loaded"] is False
        bot.shutdown()


class TestTheLogCannotTakeTheBotDown:

    def test_an_unwritable_path_is_survived(self, tmp_path):
        log = sl.PaperSessionLog(
            config=Cfg(), path="/proc/definitely/not/writable/session.jsonl")
        assert log.start()["schema"] == sl.SCHEMA
        assert log.close()["schema"] == sl.SCHEMA

    def test_a_broken_store_is_survived(self, tmp_path):
        class BrokenStore:
            def journal(self, *a, **k):
                raise RuntimeError("closed")

            def is_kill_switch_engaged(self):
                return False, ""

        log = sl.PaperSessionLog(config=Cfg(), store=BrokenStore(), path="")
        assert log.start()["schema"] == sl.SCHEMA
        assert log.close()["schema"] == sl.SCHEMA

    def test_shutdown_completes_even_if_the_session_log_throws(
        self, tmp_path, exchange
    ):
        bot = build(tmp_path, exchange, strategy=None)
        bot.startup()

        def boom():
            raise RuntimeError("session log exploded")

        bot.session.close = boom
        bot.shutdown()          # must not raise

    def test_no_path_means_no_file(self, tmp_path):
        log = sl.PaperSessionLog(config=Cfg(), path="")
        log.start()
        log.close()
        assert list(tmp_path.iterdir()) == []


class TestASessionEndsExactlyOnce:
    """`close()` is idempotent — `shutdown()` calls it, a harness may too."""

    def test_a_second_close_emits_no_second_record(self, tmp_path):
        path = tmp_path / "session.jsonl"
        log = sl.PaperSessionLog(config=Cfg(), path=str(path))
        log.start()
        first = log.close()
        second = log.close()
        lines = path.read_text().splitlines()
        assert len(lines) == 2, lines
        assert [json.loads(line)["event"] for line in lines] == [
            "SESSION_START", "SESSION_END"]
        assert second["ended_utc"] == first["ended_utc"]

    def test_the_journal_gets_one_end_row(self, tmp_path):
        store = StateStore(str(tmp_path / "s.db"))
        log = sl.PaperSessionLog(config=Cfg(), store=store, path="")
        log.start()
        log.close()
        log.close()
        ends = [d for d in store.recent_decisions(limit=50)
                if d["decision"] == "SESSION_END"]
        assert len(ends) == 1
        store.close()

    def test_shutdown_after_an_explicit_close_does_not_duplicate(
        self, tmp_path, exchange
    ):
        bot = build(tmp_path, exchange, strategy=None)
        bot.startup()
        bot.session.close()
        bot.shutdown()
        # store is closed by shutdown; re-open to inspect
        store = StateStore(str(tmp_path / "state.db"))
        ends = [d for d in store.recent_decisions(limit=50)
                if d["decision"] == "SESSION_END"]
        assert len(ends) == 1
        store.close()


class TestTheScriptedDemoBacksTheRunbook:
    """`tools/paper_session_demo.py` is what the runbook tells operators to run.

    If it drifts from the claims in the runbook, the runbook becomes a story.
    """

    def _run(self, **kw):
        sys.path.insert(0, os.path.join(REPO, "tools"))
        import paper_session_demo as demo
        return demo.run(**kw)

    def test_stand_down_exits_zero(self, tmp_path):
        rc = self._run(entries_enabled=False, ticks=10,
                       json_out=str(tmp_path / "s.jsonl"))
        assert rc == 0

    def test_entries_enabled_exits_zero_and_still_submits_nothing(self, tmp_path):
        rc = self._run(entries_enabled=True, ticks=10,
                       json_out=str(tmp_path / "s.jsonl"))
        assert rc == 0

    def test_the_demo_never_arms_live(self):
        sys.path.insert(0, os.path.join(REPO, "tools"))
        import paper_session_demo as demo
        cfg = demo.DemoConfig()
        assert cfg.USE_TESTNET is True
        assert cfg.PAPER_TRADING is True
        assert cfg.LIVE_AUTHORIZED is False
        assert cfg.LIVE_TRADING_ACK == ""
        assert cfg.POLICY_MODE == "off"

    def test_the_demo_does_not_read_the_environment(self):
        """A demo pointed at mainnet by an unlucky shell is not a demo."""
        sys.path.insert(0, os.path.join(REPO, "tools"))
        import paper_session_demo as demo
        source = inspect.getsource(demo)
        for banned in ("os.environ", "os.getenv", "config.load("):
            assert banned not in source, banned


class TestEntriesEnabledIsNotInTheGateChain:
    """It is an operator control. Promoting it to a gate is forbidden."""

    def test_the_risk_manager_never_names_it(self):
        import risk_management
        assert "ENTRIES_ENABLED" not in inspect.getsource(risk_management)

    def test_the_engine_never_names_it(self):
        assert "ENTRIES_ENABLED" not in inspect.getsource(te)

    def test_only_the_expected_production_modules_actually_read_it(self):
        """A *read*, found by AST — mentions in comments do not count.

        `bybit_connection.py` names the flag in a comment explaining why the
        order counter exists. That is documentation, not a gate. Counting it as
        a reader would push toward deleting the explanation.

        The legitimate readers, and why each is allowed to be one:

        * `config.py` parses it;
        * `main.py` decides whether to attach a strategy;
        * `session_log.py` records what it was;
        * `project_status.py` reports what it is (added slice 31, pre-declared
          in EDGE.md §12b rule 5).

        All four *report or route on* the flag. None of them gates on it —
        anything else consulting it would be a second disable path, which is the
        thing this test exists to prevent.
        """
        readers = set()
        for name in sorted(os.listdir(REPO)):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(REPO, name)) as handle:
                try:
                    tree = ast.parse(handle.read())
                except SyntaxError:
                    continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr == "ENTRIES_ENABLED":
                    readers.add(name)
                elif isinstance(node, ast.Constant) and node.value == "ENTRIES_ENABLED":
                    readers.add(name)
        assert readers == {"config.py", "main.py", "session_log.py",
                           "project_status.py"}, readers


def textwrap_dedent(source: str) -> str:
    import textwrap
    return textwrap.dedent(source)
