"""tools/testnet_session.py — refuse mainnet; never set live_authorized; ledger shape."""
from __future__ import annotations

import json
import os
import sys
import types
from unittest import mock

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tools"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import testnet_session as ts  # noqa: E402


def _cfg(**over):
    base = {
        "BYBIT_VENUE": "testnet",
        "PAPER_TRADING": False,
        "BYBIT_API_KEY": "key123",
        "BYBIT_API_SECRET": "secret123",
        "CATEGORY": "spot",
        "TAKER_FEE": 0.001,
    }
    base.update(over)
    return types.SimpleNamespace(**base)


class TestRefuseMainnet:
    def test_cli_refuses_mainnet_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BYBIT_VENUE", "mainnet")
        failure = tmp_path / "fail.json"
        ledger = tmp_path / "cash_ledger.json"
        rc = ts.main([
            "--failure-out", str(failure),
            "--ledger", str(ledger),
            "--state-db", str(tmp_path / "s.db"),
            "--evidence-path", str(tmp_path / "e.jsonl"),
            "--runs-dir", str(tmp_path / "runs"),
            "--runs-index", str(tmp_path / "runs" / "index.jsonl"),
        ])
        assert rc == 1
        payload = json.loads(failure.read_text())
        assert "mainnet" in payload["failure"].lower()
        assert payload["allows_live"] is False
        assert payload["live_authorized"] is False
        assert not ledger.exists()

    def test_cli_refuses_demo_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BYBIT_VENUE", "demo")
        failure = tmp_path / "fail.json"
        rc = ts.main([
            "--failure-out", str(failure),
            "--ledger", str(tmp_path / "cash_ledger.json"),
            "--state-db", str(tmp_path / "s.db"),
            "--evidence-path", str(tmp_path / "e.jsonl"),
            "--runs-dir", str(tmp_path / "runs"),
            "--runs-index", str(tmp_path / "runs" / "index.jsonl"),
        ])
        assert rc == 1
        assert "demo" in json.loads(failure.read_text())["failure"].lower()


class TestNeverSetsLiveAuthorized:
    def test_source_does_not_assign_live_authorized(self):
        src = open(ts.__file__, encoding="utf-8").read()
        # Must never arm live. Docstring may name the forbidden tokens;
        # executable assignments / env writes of them must not appear.
        import ast
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    name = getattr(t, "id", None) or getattr(
                        getattr(t, "attr", None), "lower", lambda: None)()
                    if isinstance(t, ast.Attribute):
                        name = t.attr
                    if name in ("live_authorized", "allows_live"):
                        # only False / falsey constant writes are ok
                        if isinstance(node.value, ast.Constant) and node.value.value is False:
                            continue
                        if isinstance(node.value, ast.NameConstant) and node.value.value is False:
                            continue
                        pytest.fail(f"assigns {name} to non-False")
            if isinstance(node, ast.Call):
                # os.environ[...] = ... patterns via subscript store
                pass
        assert "LIVE_TRADING_ACK=" not in src
        assert 'os.environ["LIVE_TRADING_ACK"]' not in src
        assert "allows_live = True" not in src
        assert "live_authorized = True" not in src

    def test_ledger_always_allows_live_false(self):
        ledger = ts.build_cash_ledger(
            symbol="BTCUSDT", category="spot", n_fills=2,
            entry_price=100.0, exit_price=101.0, qty=1.0, flat=True)
        assert ledger["allows_live"] is False
        assert set([
            "date_utc", "category", "symbol", "n_fills", "pnl_usd",
            "fees_usd", "flat", "allows_live",
        ]).issubset(ledger.keys())


class TestEvidenceAttribution:
    """A ledger/failure file with no run_id, commit, or order identity is
    not attributable to the run that produced it — it reads the same as
    any other run or a stale leftover. These lock in the fields a reader
    needs to tell them apart."""

    def test_ledger_carries_run_id_and_code_commit(self):
        ledger = ts.build_cash_ledger(
            symbol="BTCUSDT", category="spot", n_fills=2,
            entry_price=100.0, exit_price=101.0, qty=1.0, flat=True,
            run_id="RUN-1", code_commit="deadbeef" * 5)
        assert ledger["run_id"] == "RUN-1"
        assert ledger["code_commit"] == "deadbeef" * 5

    def test_ledger_without_explicit_commit_still_stamps_a_sentinel(self):
        ledger = ts.build_cash_ledger(
            symbol="BTCUSDT", category="spot", n_fills=2,
            entry_price=100.0, exit_price=101.0, qty=1.0, flat=True)
        # Never silently absent: a caller that forgets to pass code_commit
        # gets an explicit sentinel, never an empty/missing field a reader
        # could mistake for "clean".
        assert ledger["code_commit"]

    def test_ledger_carries_order_identity(self):
        ledger = ts.build_cash_ledger(
            symbol="BTCUSDT", category="spot", n_fills=2,
            entry_price=100.0, exit_price=101.0, qty=1.0, flat=True,
            entry_order_link_id="BB-entr-1",
            stop_order_link_id="BB-stop-1",
            take_profit_ids=("BB-tp-1",),
            exit_order_link_id="BB-clos-1",
            exit_order_id="2315982563845695488",
        )
        assert ledger["entry_order_link_id"] == "BB-entr-1"
        assert ledger["stop_order_link_id"] == "BB-stop-1"
        assert ledger["take_profit_ids"] == ["BB-tp-1"]
        assert ledger["exit_order_link_id"] == "BB-clos-1"
        assert ledger["exit_order_id"] == "2315982563845695488"

    def test_two_run_ids_never_collide(self):
        assert ts.new_run_id() != ts.new_run_id()

    def test_failure_payload_carries_run_id_and_code_commit(self, tmp_path):
        path = ts.write_failure(
            str(tmp_path / "fail.json"), reason="boom",
            run_id="RUN-2", code_commit="cafebabe" * 5,
            runs_dir=str(tmp_path / "runs"),
            runs_index=str(tmp_path / "runs" / "index.jsonl"))
        payload = json.loads(open(path, encoding="utf-8").read())
        assert payload["run_id"] == "RUN-2"
        assert payload["code_commit"] == "cafebabe" * 5

    def test_failure_payload_without_explicit_commit_still_stamps_a_sentinel(
            self, tmp_path):
        path = ts.write_failure(
            str(tmp_path / "fail.json"), reason="boom", run_id="RUN-X",
            runs_dir=str(tmp_path / "runs"),
            runs_index=str(tmp_path / "runs" / "index.jsonl"))
        payload = json.loads(open(path, encoding="utf-8").read())
        assert payload["code_commit"]

    def test_run_session_surfaces_order_identity_on_success(self, monkeypatch):
        fake_cfg = _cfg()
        fake_store = types.SimpleNamespace(
            open_positions=lambda: [
                {"symbol": "BTCUSDT", "entry_price": 100.0, "qty": 1.0}],
        )
        fake_client = types.SimpleNamespace(
            base_url="https://api-testnet.bybit.com",
            get_last_price=lambda s: 100.0,
            cancel_all=lambda s: None,
        )

        def fake_execute(intent):
            return types.SimpleNamespace(
                ok=True, stage="complete", reason="ENTERED_AND_PROTECTED",
                qty=1.0, entry_order_link_id="BB-entr-9",
                stop_order_link_id="BB-stop-9",
                take_profit_ids=("BB-tp-9",), detail={})

        def fake_close(*, symbol, reason):
            return types.SimpleNamespace(
                ok=True, reason="CLOSED", detail={
                    "exit_avg_price": 101.0, "executed_exit_qty": 1.0,
                    "gross_pnl": 1.0, "exit_order_link_id": "BB-clos-9",
                    "exit_order_id": "999",
                })

        fake_engine = types.SimpleNamespace(
            paper=False, sizer=None,
            execute=fake_execute, close_position=fake_close)

        import testnet_conformance_run as tcr
        monkeypatch.setattr(
            tcr, "run_preflight",
            lambda cfg, client: types.SimpleNamespace(ok=True, checks={}))
        monkeypatch.setattr(
            ts, "position_is_flat",
            lambda client, store, *, symbol, run_entry_qty, run_exit_qty: (
                True, {}))

        result = ts.run_session(
            cfg=fake_cfg, store=fake_store, client=fake_client,
            engine=fake_engine, symbol="BTCUSDT")
        assert result["entry_order_link_id"] == "BB-entr-9"
        assert result["stop_order_link_id"] == "BB-stop-9"
        assert result["take_profit_ids"] == ("BB-tp-9",)
        assert result["exit_order_link_id"] == "BB-clos-9"
        assert result["exit_order_id"] == "999"

    def test_run_session_surfaces_fee_reconciliation_from_close_detail(
            self, monkeypatch):
        """close.detail is the authority for BOTH fee legs (it re-reads
        entry fee from position meta and reads exit fee from the exit
        fill) — run_session must take them from there, not re-derive."""
        fake_cfg = _cfg()
        fake_store = types.SimpleNamespace(
            open_positions=lambda: [
                {"symbol": "BTCUSDT", "entry_price": 100.0, "qty": 1.0}],
        )
        fake_client = types.SimpleNamespace(
            base_url="https://api-testnet.bybit.com",
            get_last_price=lambda s: 100.0, cancel_all=lambda s: None,
        )
        fake_engine = types.SimpleNamespace(
            paper=False, sizer=None,
            execute=lambda intent: types.SimpleNamespace(
                ok=True, stage="complete", reason="ENTERED_AND_PROTECTED",
                qty=1.0, entry_order_link_id="e", stop_order_link_id="s",
                take_profit_ids=(), detail={"entry_fee": 0.04}),
            close_position=lambda *, symbol, reason: types.SimpleNamespace(
                ok=True, reason="CLOSED", detail={
                    "exit_avg_price": 101.0, "executed_exit_qty": 1.0,
                    "gross_pnl": 1.0, "exit_order_link_id": "x",
                    "exit_order_id": "1",
                    "entry_fee": 0.04, "entry_fee_known": True,
                    "exit_fee": 0.011, "exit_fee_known": True,
                }),
        )
        import testnet_conformance_run as tcr
        monkeypatch.setattr(
            tcr, "run_preflight",
            lambda cfg, client: types.SimpleNamespace(ok=True, checks={}))
        monkeypatch.setattr(
            ts, "position_is_flat",
            lambda client, store, *, symbol, run_entry_qty, run_exit_qty: (
                True, {}))
        result = ts.run_session(
            cfg=fake_cfg, store=fake_store, client=fake_client,
            engine=fake_engine, symbol="BTCUSDT")
        assert result["entry_fee"] == 0.04
        assert result["entry_fee_known"] is True
        assert result["exit_fee"] == 0.011
        assert result["exit_fee_known"] is True

    def test_exception_after_entry_submission_preserves_order_identity(
            self, monkeypatch):
        """Requirement: a Python exception raised AFTER a real network
        submission must not erase the order identity that submission
        produced. Here `close_position` (the flatten call) raises; the
        entry's orderLinkId/stop/fee were already captured and must
        survive into `run_session`'s return value."""
        fake_cfg = _cfg()
        fake_store = types.SimpleNamespace(
            open_positions=lambda: [
                {"symbol": "BTCUSDT", "entry_price": 100.0, "qty": 1.0}],
        )
        fake_client = types.SimpleNamespace(
            base_url="https://api-testnet.bybit.com",
            get_last_price=lambda s: 100.0, cancel_all=lambda s: None,
        )

        def fake_execute(intent):
            return types.SimpleNamespace(
                ok=True, stage="complete", reason="ENTERED_AND_PROTECTED",
                qty=1.0, entry_order_link_id="BB-entr-crash",
                stop_order_link_id="BB-stop-crash",
                take_profit_ids=(), detail={"entry_fee": 0.01})

        def fake_close(*, symbol, reason):
            raise RuntimeError("simulated network drop mid-close")

        fake_engine = types.SimpleNamespace(
            paper=False, sizer=None,
            execute=fake_execute, close_position=fake_close)

        import testnet_conformance_run as tcr
        monkeypatch.setattr(
            tcr, "run_preflight",
            lambda cfg, client: types.SimpleNamespace(ok=True, checks={}))

        result = ts.run_session(
            cfg=fake_cfg, store=fake_store, client=fake_client,
            engine=fake_engine, symbol="BTCUSDT")

        assert result["ok"] is False
        assert result["entry_order_link_id"] == "BB-entr-crash"
        assert result["stop_order_link_id"] == "BB-stop-crash"
        assert result["entry_fee"] == 0.01
        assert result["entry_fee_known"] is True
        assert "RuntimeError" in result["exception"]
        assert "simulated network drop" in result["failure"]

    def test_exception_before_any_flatten_attempt_tries_a_best_effort_close(
            self, monkeypatch):
        """A crash between the entry report coming back and the flatten
        call being reached (e.g. reading the position row) must still
        attempt one best-effort close — never silently leave a
        known-submitted entry naked with no attempt made at all."""
        fake_cfg = _cfg()

        class ExplodingStore:
            def open_positions(self):
                raise RuntimeError("store read failed")

        fake_client = types.SimpleNamespace(
            base_url="https://api-testnet.bybit.com",
            get_last_price=lambda s: 100.0, cancel_all=lambda s: None,
        )
        close_calls = []

        def fake_close(*, symbol, reason):
            close_calls.append(reason)
            return types.SimpleNamespace(ok=True, reason="CLOSED", detail={})

        fake_engine = types.SimpleNamespace(
            paper=False, sizer=None,
            execute=lambda intent: types.SimpleNamespace(
                ok=True, stage="complete", reason="ENTERED_AND_PROTECTED",
                qty=1.0, entry_order_link_id="BB-entr-exploding",
                stop_order_link_id="", take_profit_ids=(), detail={}),
            close_position=fake_close)

        import testnet_conformance_run as tcr
        monkeypatch.setattr(
            tcr, "run_preflight",
            lambda cfg, client: types.SimpleNamespace(ok=True, checks={}))

        result = ts.run_session(
            cfg=fake_cfg, store=ExplodingStore(), client=fake_client,
            engine=fake_engine, symbol="BTCUSDT")

        assert result["entry_order_link_id"] == "BB-entr-exploding"
        assert close_calls == ["session_error_flatten"]
        assert result["flatten_attempted"] is True


class TestRunScopedImmutableRecords:
    """Requirement: a run_id's record is written once and never silently
    overwritten by a different run; the top-level cash_ledger.json/
    testnet_session_failure.json singleton is a DERIVED latest view only."""

    def test_two_runs_never_overwrite_each_others_record(self, tmp_path):
        runs_dir = str(tmp_path / "runs")
        index_path = str(tmp_path / "runs" / "index.jsonl")
        first = ts.build_cash_ledger(
            symbol="BTCUSDT", category="spot", n_fills=2,
            entry_price=100.0, exit_price=101.0, qty=1.0, flat=True,
            run_id="RUN-A", code_commit="a" * 40)
        second = ts.build_cash_ledger(
            symbol="BTCUSDT", category="spot", n_fills=2,
            entry_price=200.0, exit_price=201.0, qty=2.0, flat=True,
            run_id="RUN-B", code_commit="b" * 40)
        path_a = ts.write_run_scoped_record(
            first, run_id="RUN-A", kind="ledger",
            runs_dir=runs_dir, index_path=index_path)
        path_b = ts.write_run_scoped_record(
            second, run_id="RUN-B", kind="ledger",
            runs_dir=runs_dir, index_path=index_path)
        assert path_a != path_b
        recovered_a = json.loads(open(path_a, encoding="utf-8").read())
        recovered_b = json.loads(open(path_b, encoding="utf-8").read())
        # RUN-A's record is still RUN-A's — writing RUN-B never touched it.
        assert recovered_a["entry_price"] == 100.0
        assert recovered_b["entry_price"] == 200.0
        index_lines = [
            json.loads(line) for line in
            open(index_path, encoding="utf-8").read().splitlines() if line]
        assert [e["run_id"] for e in index_lines] == ["RUN-A", "RUN-B"]

    def test_same_run_ledger_then_failure_coexist_as_separate_records(
            self, tmp_path):
        """The `not ledger['flat']` path writes a ledger AND a failure for
        the SAME run_id — both facts about that run must survive, not the
        second overwriting the first."""
        runs_dir = str(tmp_path / "runs")
        index_path = str(tmp_path / "runs" / "index.jsonl")
        ledger = ts.build_cash_ledger(
            symbol="BTCUSDT", category="spot", n_fills=2,
            entry_price=100.0, exit_price=101.0, qty=1.0, flat=False,
            run_id="RUN-C", code_commit="c" * 40)
        ts.write_run_scoped_record(
            ledger, run_id="RUN-C", kind="ledger",
            runs_dir=runs_dir, index_path=index_path)
        ts.write_failure(
            str(tmp_path / "fail.json"), reason="position not flat after session",
            run_id="RUN-C", code_commit="c" * 40,
            runs_dir=runs_dir, runs_index=index_path)
        run_dir = os.path.join(runs_dir, "RUN-C")
        assert os.path.exists(os.path.join(run_dir, "ledger.json"))
        assert os.path.exists(os.path.join(run_dir, "failure.json"))

    def test_latest_view_is_marked_derived_and_points_at_the_run_record(
            self, tmp_path):
        path = ts.write_failure(
            str(tmp_path / "fail.json"), reason="boom", run_id="RUN-D",
            runs_dir=str(tmp_path / "runs"),
            runs_index=str(tmp_path / "runs" / "index.jsonl"))
        payload = json.loads(open(path, encoding="utf-8").read())
        assert payload["artifact_role"] == "latest_view"
        assert payload["run_manifest_path"].endswith("failure.json")
        assert "RUN-D" in payload["run_manifest_path"]

    # -- genuine immutability: exclusive creation, not exists()->write() --

    def test_first_write_succeeds(self, tmp_path):
        runs_dir = str(tmp_path / "runs")
        index_path = str(tmp_path / "runs" / "index.jsonl")
        path = ts.write_run_scoped_record(
            {"schema": "cash_ledger/2", "pnl_usd": 1.0},
            run_id="RUN-E", kind="ledger", runs_dir=runs_dir,
            index_path=index_path)
        assert os.path.exists(path)

    def test_second_write_to_the_same_run_id_and_kind_raises(self, tmp_path):
        runs_dir = str(tmp_path / "runs")
        index_path = str(tmp_path / "runs" / "index.jsonl")
        ts.write_run_scoped_record(
            {"pnl_usd": 1.0}, run_id="RUN-F", kind="ledger",
            runs_dir=runs_dir, index_path=index_path)
        with pytest.raises(ts.RunRecordAlreadyExists):
            ts.write_run_scoped_record(
                {"pnl_usd": 999.0}, run_id="RUN-F", kind="ledger",
                runs_dir=runs_dir, index_path=index_path)

    def test_file_contents_remain_the_original_bytes_after_a_refused_rewrite(
            self, tmp_path):
        runs_dir = str(tmp_path / "runs")
        index_path = str(tmp_path / "runs" / "index.jsonl")
        path = ts.write_run_scoped_record(
            {"pnl_usd": 1.0}, run_id="RUN-G", kind="ledger",
            runs_dir=runs_dir, index_path=index_path)
        original_bytes = open(path, "rb").read()
        try:
            ts.write_run_scoped_record(
                {"pnl_usd": 999.0}, run_id="RUN-G", kind="ledger",
                runs_dir=runs_dir, index_path=index_path)
        except ts.RunRecordAlreadyExists:
            pass
        assert open(path, "rb").read() == original_bytes

    def test_same_run_different_kinds_both_succeed(self, tmp_path):
        runs_dir = str(tmp_path / "runs")
        index_path = str(tmp_path / "runs" / "index.jsonl")
        ts.write_run_scoped_record(
            {"pnl_usd": 1.0}, run_id="RUN-H", kind="ledger",
            runs_dir=runs_dir, index_path=index_path)
        path2 = ts.write_run_scoped_record(
            {"failure": "x"}, run_id="RUN-H", kind="failure",
            runs_dir=runs_dir, index_path=index_path)
        assert os.path.exists(path2)

    def test_different_runs_same_kind_both_succeed(self, tmp_path):
        runs_dir = str(tmp_path / "runs")
        index_path = str(tmp_path / "runs" / "index.jsonl")
        path1 = ts.write_run_scoped_record(
            {"pnl_usd": 1.0}, run_id="RUN-I1", kind="ledger",
            runs_dir=runs_dir, index_path=index_path)
        path2 = ts.write_run_scoped_record(
            {"pnl_usd": 2.0}, run_id="RUN-I2", kind="ledger",
            runs_dir=runs_dir, index_path=index_path)
        assert path1 != path2
        assert os.path.exists(path1) and os.path.exists(path2)

    def test_duplicate_index_identity_rejected_no_extra_line_appended(
            self, tmp_path):
        """A refused second write must not append a duplicate index
        record for the same (run_id, kind) — the index write only
        happens after the exclusive file create already succeeded."""
        runs_dir = str(tmp_path / "runs")
        index_path = str(tmp_path / "runs" / "index.jsonl")
        ts.write_run_scoped_record(
            {"pnl_usd": 1.0}, run_id="RUN-J", kind="ledger",
            runs_dir=runs_dir, index_path=index_path)
        try:
            ts.write_run_scoped_record(
                {"pnl_usd": 2.0}, run_id="RUN-J", kind="ledger",
                runs_dir=runs_dir, index_path=index_path)
        except ts.RunRecordAlreadyExists:
            pass
        lines = [
            json.loads(line) for line in
            open(index_path, encoding="utf-8").read().splitlines() if line]
        matching = [e for e in lines
                   if e["run_id"] == "RUN-J" and e["kind"] == "ledger"]
        assert len(matching) == 1

    def test_restart_style_repetition_cannot_overwrite_an_existing_record(
            self, tmp_path):
        """Simulates a process restart that re-derives the SAME run_id
        (e.g. a bug, or a deliberate retry with a reused identifier) and
        tries to write the same record again — must be refused, not
        silently accepted as "latest value wins"."""
        runs_dir = str(tmp_path / "runs")
        index_path = str(tmp_path / "runs" / "index.jsonl")
        original = {"pnl_usd": 1.0, "note": "first boot"}
        ts.write_run_scoped_record(
            original, run_id="RUN-RESTART", kind="ledger",
            runs_dir=runs_dir, index_path=index_path)
        with pytest.raises(ts.RunRecordAlreadyExists):
            ts.write_run_scoped_record(
                {"pnl_usd": 2.0, "note": "restarted boot"},
                run_id="RUN-RESTART", kind="ledger",
                runs_dir=runs_dir, index_path=index_path)
        on_disk = json.loads(open(
            os.path.join(runs_dir, "RUN-RESTART", "ledger.json"),
            encoding="utf-8").read())
        assert on_disk["note"] == "first boot"

    def test_concurrent_duplicate_attempt_cannot_produce_two_canonical_records(
            self, tmp_path):
        """Exclusive creation (O_CREAT|O_EXCL) is atomic at the OS level:
        of two attempts to create the same path, exactly one succeeds —
        there is no `exists()`-then-`write()` window for both to pass the
        check before either writes."""
        runs_dir = str(tmp_path / "runs")
        index_path = str(tmp_path / "runs" / "index.jsonl")
        results = []
        for payload in ({"pnl_usd": 1.0}, {"pnl_usd": 2.0}):
            try:
                ts.write_run_scoped_record(
                    payload, run_id="RUN-RACE", kind="ledger",
                    runs_dir=runs_dir, index_path=index_path)
                results.append("ok")
            except ts.RunRecordAlreadyExists:
                results.append("refused")
        assert results == ["ok", "refused"]
        # Exactly one record exists, with exactly one winner's content.
        assert os.path.exists(os.path.join(runs_dir, "RUN-RACE", "ledger.json"))


class TestFeeReconciliationInLedger:
    """build_cash_ledger must prefer authoritative venue fee data over a
    static taker-fee estimate, say which one it used, and NEVER add a raw
    BTC amount to a raw USDT amount as though they were the same number.
    Case letters below match the hardening-pass requirement matrix."""

    # -- CASE B: both known, same accounting unit (USDT/USDT) -----------
    def test_case_b_both_legs_known_same_unit_reports_fees_source_venue(self):
        ledger = ts.build_cash_ledger(
            symbol="BTCUSDT", category="spot", n_fills=2,
            entry_price=100.0, exit_price=101.0, qty=1.0, flat=True,
            entry_fee=0.05, entry_fee_known=True, entry_fee_currency="USDT",
            exit_fee=0.03, exit_fee_known=True, exit_fee_currency="USDT",
            taker_fee=0.999)
        assert ledger["fees_source"] == "venue"
        assert ledger["fees_realized"] is True
        # 0.999 taker_fee would make the estimate enormous — proves the
        # static estimate was never consulted when both legs are known.
        assert ledger["fees_account_unit"] == 0.08
        assert ledger["accounting_unit"] == "USDT"
        assert ledger["entry_fee_known"] is True
        assert ledger["exit_fee_known"] is True

    # -- CASE A / H: real BTCUSDT shape — BTC entry fee, USDT exit fee --
    def test_case_a_btc_entry_fee_usdt_exit_fee_converts_not_adds_raw(self):
        ledger = ts.build_cash_ledger(
            symbol="BTCUSDT", category="spot", n_fills=2,
            entry_price=100.0, exit_price=101.0, qty=1.0, flat=True,
            entry_fee=0.00001, entry_fee_known=True, entry_fee_currency="BTC",
            exit_fee=0.03, exit_fee_known=True, exit_fee_currency="USDT",
            taker_fee=0.001)
        assert ledger["fees_source"] == "venue"
        assert ledger["fees_realized"] is True
        # entry: 0.00001 BTC * 100.0 (its own fill price) = 0.001 USDT
        assert ledger["entry_fee_account_unit"] == 0.001
        assert ledger["entry_fee_conversion"]["status"] == "converted"
        assert ledger["entry_fee_conversion"]["rate"] == 100.0
        # exit: already USDT — identity, no multiplication
        assert ledger["exit_fee_account_unit"] == 0.03
        assert ledger["exit_fee_conversion"]["status"] == "identity"
        assert ledger["fees_account_unit"] == round(0.001 + 0.03, 8)
        # The raw amounts (0.00001 and 0.03) were NEVER added directly —
        # that would be 0.03001, numerically close but conceptually wrong
        # and a coincidence of this example's small numbers, not a
        # guarantee; the real proof is `rate == 100.0` above showing the
        # conversion actually multiplied through before summing.
        assert ledger["entry_fee_amount"] == 0.00001
        assert ledger["entry_fee_currency"] == "BTC"
        assert ledger["exit_fee_amount"] == 0.03
        assert ledger["exit_fee_currency"] == "USDT"

    # -- CASE D: both unknown -----------------------------------------
    def test_case_d_both_legs_unknown_falls_back_to_static_estimate(self):
        ledger = ts.build_cash_ledger(
            symbol="BTCUSDT", category="spot", n_fills=2,
            entry_price=100.0, exit_price=101.0, qty=1.0, flat=True,
            taker_fee=0.001)
        assert ledger["fees_source"] == "estimated"
        assert ledger["fees_realized"] is False
        assert ledger["fees_account_unit"] == round(
            100.0 * 1.0 * 0.001 + 101.0 * 1.0 * 0.001, 8)
        assert ledger["entry_fee_known"] is False
        assert ledger["exit_fee_known"] is False

    # -- CASE C: one known, one unknown ---------------------------------
    def test_case_c_one_leg_known_one_estimated_is_reported_as_partial(self):
        ledger = ts.build_cash_ledger(
            symbol="BTCUSDT", category="spot", n_fills=2,
            entry_price=100.0, exit_price=101.0, qty=1.0, flat=True,
            entry_fee=0.05, entry_fee_known=True, entry_fee_currency="USDT",
            taker_fee=0.001)
        assert ledger["fees_source"] == "partial_venue_partial_estimated"
        assert ledger["fees_realized"] is False
        assert ledger["entry_fee_known"] is True
        assert ledger["exit_fee_known"] is False
        # entry leg is the venue's 0.05, exit leg is the static estimate —
        # the aggregate must NOT silently read as fully known.
        assert ledger["entry_fee_account_unit"] == 0.05
        assert ledger["exit_fee_account_unit"] == round(101.0 * 1.0 * 0.001, 8)

    # -- CASE E / G: malformed / foreign currency, no conversion basis --
    def test_case_e_malformed_currency_rejects_conversion_no_fake_number(self):
        ledger = ts.build_cash_ledger(
            symbol="BTCUSDT", category="spot", n_fills=2,
            entry_price=100.0, exit_price=101.0, qty=1.0, flat=True,
            entry_fee=0.05, entry_fee_known=True, entry_fee_currency="ETH",
            exit_fee=0.03, exit_fee_known=True, exit_fee_currency="USDT",
            taker_fee=0.001)
        assert ledger["fees_source"] == "unresolved_mixed_unit"
        assert ledger["fees_realized"] is False
        assert ledger["entry_fee_conversion"]["status"] == "unresolved"
        # The raw fact is preserved — never dropped, never guessed into a
        # number this ledger cannot back up.
        assert ledger["entry_fee_amount"] == 0.05
        assert ledger["entry_fee_currency"] == "ETH"
        assert ledger["entry_fee_account_unit"] is None
        assert ledger["entry_fee_usd"] is None

    def test_case_g_amount_known_currency_unknown_rejects_conversion(self):
        ledger = ts.build_cash_ledger(
            symbol="BTCUSDT", category="spot", n_fills=2,
            entry_price=100.0, exit_price=101.0, qty=1.0, flat=True,
            entry_fee=0.05, entry_fee_known=True, entry_fee_currency=None,
            exit_fee=0.03, exit_fee_known=True, exit_fee_currency="USDT",
            taker_fee=0.001)
        assert ledger["fees_source"] == "unresolved_mixed_unit"
        assert ledger["entry_fee_conversion"]["status"] == "unresolved"
        assert ledger["entry_fee_account_unit"] is None

    # -- CASE F: a currency that matches the base coin but with no usable
    #    price to convert at (invalid conversion rate) --------------------
    def test_case_f_invalid_conversion_rate_is_rejected(self):
        ledger = ts.build_cash_ledger(
            symbol="BTCUSDT", category="spot", n_fills=2,
            entry_price=0.0, exit_price=101.0, qty=1.0, flat=True,
            entry_fee=0.00001, entry_fee_known=True, entry_fee_currency="BTC",
            exit_fee=0.03, exit_fee_known=True, exit_fee_currency="USDT",
            taker_fee=0.001)
        assert ledger["entry_fee_conversion"]["status"] == "unresolved"
        assert ledger["entry_fee_account_unit"] is None
        assert ledger["fees_source"] == "unresolved_mixed_unit"

    # -- CASE J / K: known zero vs. unknown must stay distinct ----------
    def test_case_j_known_zero_fee_is_realized_not_estimated(self):
        ledger = ts.build_cash_ledger(
            symbol="BTCUSDT", category="spot", n_fills=2,
            entry_price=100.0, exit_price=101.0, qty=1.0, flat=True,
            entry_fee=0.0, entry_fee_known=True, entry_fee_currency="USDT",
            exit_fee=0.0, exit_fee_known=True, exit_fee_currency="USDT")
        assert ledger["fees_source"] == "venue"
        assert ledger["fees_realized"] is True
        assert ledger["entry_fee_account_unit"] == 0.0
        assert ledger["entry_fee_conversion"]["status"] == "identity"

    def test_case_k_absent_fee_is_unknown_not_known_zero(self):
        ledger = ts.build_cash_ledger(
            symbol="BTCUSDT", category="spot", n_fills=2,
            entry_price=100.0, exit_price=101.0, qty=1.0, flat=True,
            taker_fee=0.001)
        assert ledger["entry_fee_known"] is False
        assert ledger["entry_fee_amount"] is None
        assert ledger["entry_fee_conversion"]["status"] == "unknown"

    # -- CASE I: conversion metadata must always be reproducible --------
    def test_case_i_conversion_basis_is_never_empty_when_a_leg_had_qty(self):
        ledger = ts.build_cash_ledger(
            symbol="BTCUSDT", category="spot", n_fills=2,
            entry_price=100.0, exit_price=101.0, qty=1.0, flat=True,
            entry_fee=0.00001, entry_fee_known=True, entry_fee_currency="BTC",
            exit_fee=0.03, exit_fee_known=True, exit_fee_currency="USDT")
        assert ledger["entry_fee_conversion"]["basis"]
        assert ledger["exit_fee_conversion"]["basis"]

    def test_accounting_unit_is_never_labelled_usd_without_evidence(self):
        ledger = ts.build_cash_ledger(
            symbol="BTCUSDT", category="spot", n_fills=2,
            entry_price=100.0, exit_price=101.0, qty=1.0, flat=True)
        assert ledger["accounting_unit"] == "USDT"
        assert ledger["accounting_unit"] != "USD"


class TestProvenancePrecision:
    """Requirement: a `-dirty` workspace must never be misread as the
    SOURCE CODE itself differing from its claimed commit — a stray
    untracked artefact is not the same fact as a modified .py file."""

    def _git_repo(self, tmp_path):
        import subprocess
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@example.com"],
                       cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
        (repo / "main.py").write_text("print('hi')\n")
        (repo / "requirements.txt").write_text("requests==2.31.0\n")
        subprocess.run(["git", "add", "main.py", "requirements.txt"],
                       cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
        return repo

    def test_untracked_non_source_file_is_workspace_dirty_but_source_clean(
            self, tmp_path):
        import sys as _sys
        _sys.path.insert(0, os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
        import provenance as prov
        repo = self._git_repo(tmp_path)
        (repo / "cash_ledger.json").write_text("{}\n")
        assert prov.workspace_dirty(str(repo)) is True
        assert prov.source_tree_clean(str(repo)) is True

    def test_modified_tracked_source_is_both_dirty_and_unclean(self, tmp_path):
        import sys as _sys
        _sys.path.insert(0, os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
        import provenance as prov
        repo = self._git_repo(tmp_path)
        (repo / "main.py").write_text("print('changed')\n")
        assert prov.workspace_dirty(str(repo)) is True
        assert prov.source_tree_clean(str(repo)) is False

    def test_clean_repo_is_neither_dirty_nor_unclean(self, tmp_path):
        import sys as _sys
        _sys.path.insert(0, os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
        import provenance as prov
        repo = self._git_repo(tmp_path)
        assert prov.workspace_dirty(str(repo)) is False
        assert prov.source_tree_clean(str(repo)) is True

    def test_modified_execution_relevant_non_python_input_is_unclean(
            self, tmp_path):
        """requirements.txt pins the exact dependency versions this
        process's import machinery resolves against — a version bump
        changes behaviour exactly as a `.py` edit would, with no line in
        any `.py` diff to show it. `source_tree_clean` must catch it."""
        import sys as _sys
        _sys.path.insert(0, os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
        import provenance as prov
        repo = self._git_repo(tmp_path)
        (repo / "requirements.txt").write_text("requests==99.0.0\n")
        assert prov.workspace_dirty(str(repo)) is True
        assert prov.source_tree_clean(str(repo)) is False

    def test_mixed_source_and_artifact_dirtiness_is_not_conflated(
            self, tmp_path):
        """One tracked source file modified AND one untracked artefact
        present at the same time: both facts must be visible and
        distinct, not collapsed into a single signal."""
        import sys as _sys
        _sys.path.insert(0, os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
        import provenance as prov
        repo = self._git_repo(tmp_path)
        (repo / "main.py").write_text("print('changed')\n")
        (repo / "cash_ledger.json").write_text("{}\n")
        assert prov.workspace_dirty(str(repo)) is True
        assert prov.source_tree_clean(str(repo)) is False

    def test_ledger_code_commit_is_never_suffixed_with_dirty(self):
        """`code_commit` must be a bare sha; `-dirty` belongs only to the
        separate `workspace_dirty`/`source_tree_clean` fields."""
        ledger = ts.build_cash_ledger(
            symbol="BTCUSDT", category="spot", n_fills=2,
            entry_price=100.0, exit_price=101.0, qty=1.0, flat=True,
            code_commit="f" * 40, source_tree_clean=False,
            workspace_dirty=True)
        assert ledger["code_commit"] == "f" * 40
        assert not ledger["code_commit"].endswith("-dirty")
        assert ledger["source_tree_clean"] is False
        assert ledger["workspace_dirty"] is True


class TestNotionalCapThisPathOnly:
    def test_capped_sizer_reduces_qty(self):
        from position_sizing import SizingResult, SizingRequest

        class Inner:
            def calculate_position_size(self, request):
                return SizingResult(
                    symbol=request.symbol, qty=1.0, notional=100000.0,
                    reason="OK", method="test")

        capped = ts._CappedSizer(Inner(), max_notional_usd=1000.0)
        req = SizingRequest(
            symbol="BTCUSDT", side="Buy", current_price=100000.0,
            current_portfolio_value=10000.0, stop_loss_distance=0.02)
        # SizingRequest may need filters — construct minimally
        try:
            out = capped.calculate_position_size(req)
        except TypeError:
            # If SizingRequest signature differs, use a SimpleNamespace.
            req = types.SimpleNamespace(
                symbol="BTCUSDT", current_price=100000.0, filters=None)
            out = capped.calculate_position_size(req)
        assert out.qty <= 1000.0 / 100000.0 + 1e-12
        assert out.qty < 1.0


class TestSubmitAndFlattenNames:
    def test_submit_order_calls_engine_execute(self):
        calls = {}

        class Eng:
            def execute(self, intent):
                calls["intent"] = intent
                return types.SimpleNamespace(
                    ok=True, stage="complete", reason="ENTERED",
                    qty=0.01, detail={})

        ts.submit_order(
            Eng(), symbol="BTCUSDT", entry_price=100.0,
            stop_price=98.0, take_profit=105.0)
        assert calls["intent"].signal_type == "BUY"
        assert calls["intent"].symbol == "BTCUSDT"

    def test_flatten_calls_close_position(self):
        calls = {}

        class Eng:
            def close_position(self, *, symbol, reason):
                calls["symbol"] = symbol
                calls["reason"] = reason
                return types.SimpleNamespace(
                    ok=True, reason="CLOSED", detail={})

        ts.flatten_position(Eng(), symbol="BTCUSDT")
        assert calls["symbol"] == "BTCUSDT"


class TestBookkeepingFailureSurvivability:
    """requirement: exception during ledger writing / latest-view writing
    / index writing must not erase the order identity a real session
    already produced — a failure.json must still carry it."""

    def _full_success_stack(self):
        fake_cfg = _cfg()
        fake_store = types.SimpleNamespace(
            open_positions=lambda: [
                {"symbol": "BTCUSDT", "entry_price": 100.0, "qty": 1.0}],
            close=lambda: None,
        )
        fake_client = types.SimpleNamespace(
            base_url="https://api-testnet.bybit.com",
            get_last_price=lambda s: 100.0, cancel_all=lambda s: None,
        )
        fake_engine = types.SimpleNamespace(
            paper=False, sizer=None,
            execute=lambda intent: types.SimpleNamespace(
                ok=True, stage="complete", reason="ENTERED_AND_PROTECTED",
                qty=1.0, entry_order_link_id="BB-entr-bk",
                stop_order_link_id="BB-stop-bk", take_profit_ids=(),
                detail={"entry_fee": 0.00001, "entry_fee_currency": "BTC"}),
            close_position=lambda *, symbol, reason: types.SimpleNamespace(
                ok=True, reason="CLOSED", detail={
                    "exit_avg_price": 101.0, "executed_exit_qty": 1.0,
                    "gross_pnl": 1.0, "exit_order_link_id": "BB-clos-bk",
                    "exit_order_id": "777",
                    "entry_fee": 0.00001, "entry_fee_currency": "BTC",
                    "entry_fee_known": True,
                    "exit_fee": 0.03, "exit_fee_currency": "USDT",
                    "exit_fee_known": True,
                }),
        )
        return fake_cfg, fake_store, fake_client, fake_engine

    def test_ledger_write_failure_falls_back_to_failure_json_with_identity(
            self, tmp_path, monkeypatch):
        monkeypatch.setenv("BYBIT_VENUE", "testnet")
        fake_cfg, fake_store, fake_client, fake_engine = self._full_success_stack()

        def fake_build(**kw):
            return fake_cfg, fake_store, fake_client, fake_engine, object

        monkeypatch.setattr(ts, "_build_session_stack", fake_build)
        monkeypatch.setattr(
            ts.tcr, "run_preflight",
            lambda cfg, client: types.SimpleNamespace(ok=True, checks={}))
        monkeypatch.setattr(
            ts, "position_is_flat",
            lambda client, store, *, symbol, run_entry_qty, run_exit_qty: (
                True, {}))

        original_write = ts.write_run_scoped_record

        def exploding_write(payload, *, run_id, kind, **kw):
            if kind == "ledger":
                raise OSError("simulated disk failure during ledger write")
            return original_write(payload, run_id=run_id, kind=kind, **kw)

        monkeypatch.setattr(ts, "write_run_scoped_record", exploding_write)

        failure_path = tmp_path / "fail.json"
        rc = ts.main([
            "--failure-out", str(failure_path),
            "--ledger", str(tmp_path / "cash_ledger.json"),
            "--state-db", str(tmp_path / "s.db"),
            "--evidence-path", str(tmp_path / "e.jsonl"),
            "--runs-dir", str(tmp_path / "runs"),
            "--runs-index", str(tmp_path / "runs" / "index.jsonl"),
        ])
        assert rc == 1
        payload = json.loads(failure_path.read_text())
        assert "bookkeeping raised" in payload["failure"]
        # The order identity a REAL session produced must survive the
        # bookkeeping exception that happened afterward.
        assert payload["entry_order_link_id"] == "BB-entr-bk"
        assert payload["stop_order_link_id"] == "BB-stop-bk"
        assert payload["exit_order_link_id"] == "BB-clos-bk"
        assert payload["exit_order_id"] == "777"
        assert payload["entry_fee"] == 0.00001
        assert payload["entry_fee_currency"] == "BTC"
        assert payload["exit_fee"] == 0.03
        assert payload["exit_fee_currency"] == "USDT"


class TestZeroFillsExitCode:
    def test_main_returns_2_on_zero_fills(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BYBIT_VENUE", "testnet")
        monkeypatch.delenv("USE_TESTNET", raising=False)

        fake_cfg = _cfg()
        fake_store = types.SimpleNamespace(
            open_positions=lambda: [],
            close=lambda: None,
        )
        fake_client = types.SimpleNamespace(
            base_url="https://api-testnet.bybit.com",
            get_last_price=lambda s: 100000.0,
        )
        fake_engine = types.SimpleNamespace(
            paper=False,
            sizer=None,
            execute=lambda intent: types.SimpleNamespace(
                ok=False, stage="pre_gate", reason="BLOCKED", qty=0.0,
                detail={}),
            close_position=lambda **kw: types.SimpleNamespace(
                ok=False, reason="NO_SUCH_POSITION", detail={}),
        )

        def fake_build(**kw):
            return fake_cfg, fake_store, fake_client, fake_engine, object

        def fake_preflight(cfg, client):
            return types.SimpleNamespace(ok=True, checks={"environment": "ok"})

        monkeypatch.setattr(ts, "_build_session_stack", fake_build)
        monkeypatch.setattr(ts.tcr, "run_preflight", fake_preflight)

        failure = tmp_path / "fail.json"
        rc = ts.main([
            "--failure-out", str(failure),
            "--ledger", str(tmp_path / "cash_ledger.json"),
            "--state-db", str(tmp_path / "s.db"),
            "--evidence-path", str(tmp_path / "e.jsonl"),
            "--runs-dir", str(tmp_path / "runs"),
            "--runs-index", str(tmp_path / "runs" / "index.jsonl"),
        ])
        assert rc == 2
        assert json.loads(failure.read_text())["entry_fills"] == 0
