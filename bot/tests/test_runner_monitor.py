"""The unattended default is READ-ONLY, and every outcome says what was learned.

Offline. The decision flow under test is the production one: scripted venue
fakes -> `carry_preflight.run_preflight` (-> `CarryEngine.entry_economics` ->
`carry_costs.entry_gate`) -> a JSON report -> `run_outcomes.classify_preflight`
-> `runner_monitor.monitor_once`. Nothing is re-derived in the monitor.
"""
from __future__ import annotations

import ast
import json
import os
import sys
from types import SimpleNamespace

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, BOT)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(BOT, "tools"))

import carry_costs  # noqa: E402
import run_outcomes as RO  # noqa: E402
import runner_fixtures as RF  # noqa: E402
import runner_monitor as RM  # noqa: E402
import unattended_drill as UD  # noqa: E402
from carry_broker import CarryBroker  # noqa: E402
from test_carry_broker import envelope  # noqa: E402
from test_carry_preflight_economics import Market, cfg  # noqa: E402
from tools import carry_preflight as pf  # noqa: E402

ENV = {"BYBIT_API_KEY": "k", "BYBIT_API_SECRET": "s", "PATH": "/usr/bin",
       "LIVE_TRADING_ACK": "I_UNDERSTAND", "BYBIT_VENUE": "mainnet",
       "PAPER_TRADING": "0", "USE_TESTNET": "0"}


def report_of(market, *, mode="overlay", equity=9800.0, paper=True,
              known_perp=0.0, venue="testnet"):
    """The JSON the preflight CLI would print, from the real `run_preflight`."""
    broker = CarryBroker(client=market, sequence_source=lambda *a: 0,
                         order_gate=lambda: (False, "preflight is read-only"))

    def equity_read():
        if isinstance(equity, Exception):
            raise equity
        return equity
    c = cfg(paper)
    c.BYBIT_VENUE = venue
    rep = pf.run_preflight(
        broker=broker, client=SimpleNamespace(get_equity=equity_read), cfg=c,
        spot_symbol="BTCUSDT", perp_symbol="BTCUSDT", execution_mode=mode,
        known_perp_qty=known_perp, execution_style="taker", borrow_apr=0.0)
    return json.loads(json.dumps(rep, default=str))


def outcome(rep, rc=0):
    return RO.classify_preflight(rep, rc)


class WithOpenOrders(Market):
    def _request(self, method, endpoint, **kw):
        if endpoint == "/v5/order/realtime":
            return envelope({"list": [{"orderLinkId": "carry-1", "symbol": "BTCUSDT"}]})
        return super()._request(method, endpoint, **kw)


# -- the production decision flow, classified ---------------------------------

class TestEveryOutcomeSaysWhatWasLearned:
    def test_eligible_is_monitor_ready_and_not_a_trade_signal(self):
        rep = report_of(Market(rate_bps=3.0))
        r = outcome(rep)
        assert r["outcome"] == RO.MONITOR_READY and r["order_sent"] is False
        d = r["assessment"]["entry_decision"]
        assert d["gate"]["state"] == "PASS"
        notes = " ".join(r["assessment"]["notes"])
        assert "no order was sent" in notes and "not permission to execute" in notes

    def test_an_ineligible_entry_is_a_correct_refusal_not_an_error(self):
        rep = report_of(Market(rate_bps=0.3))
        assert rep["verdict"] == "PREFLIGHT_OK"
        r = outcome(rep)
        assert r["outcome"] == RO.ENTRY_NOT_ELIGIBLE
        assert r["reasons"] == ["BREAKEVEN_HOLD_EXCEEDS_DEMONSTRATED_HOLD"]
        d = r["assessment"]["entry_decision"]
        assert d["hold_gate"]["settlements_needed"] == 37
        assert d["hold_gate"]["settlements_budget"] == 17

    @pytest.mark.parametrize("rate,reason", [
        (-10.0, "FUNDING_NOT_POSITIVE"), (0.05, "CARRY_BELOW_COST_OF_CAPITAL")])
    def test_other_economic_refusals_are_not_eligible_too(self, rate, reason):
        r = outcome(report_of(Market(rate_bps=rate)))
        assert (r["outcome"], r["reasons"]) == (RO.ENTRY_NOT_ELIGIBLE, [reason])

    def test_too_few_prints_is_missing_data_not_an_economic_judgment(self):
        r = outcome(report_of(Market(n_prints=2)))
        assert r["outcome"] == RO.MONITOR_FAILED
        assert "missing data" in " ".join(r["reasons"])

    def test_stale_inputs_fail_the_monitor_and_claim_nothing(self):
        r = outcome(report_of(Market(newest_age_h=20.0)))
        assert r["outcome"] == RO.MONITOR_FAILED
        assert r["assessment"]["entry_decision"] is None or \
            r["assessment"]["entry_decision"]["gate"]["state"] != "PASS"

    def test_unreadable_fee_rates_fail_never_assumed(self):
        r = outcome(report_of(Market(fee_fails=True)))
        assert r["outcome"] == RO.MONITOR_FAILED

    @pytest.mark.parametrize("exc", [RuntimeError("down"), TimeoutError("slow")])
    def test_unreadable_account_state_fails(self, exc):
        rep = report_of(Market(), equity=exc)
        assert rep["verdict"] == "PREFLIGHT_BLOCKED"
        r = outcome(rep, rc=2)
        assert r["outcome"] == RO.MONITOR_FAILED
        assert "equity unreadable" in " ".join(r["reasons"])

    def test_an_open_position_fails_the_safety_precondition(self):
        rep = report_of(Market(perp_qty=0.004))
        r = outcome(rep, rc=2)
        assert r["outcome"] == RO.MONITOR_FAILED
        assert "ledger knows about" in " ".join(r["reasons"])

    def test_an_open_order_fails_the_safety_precondition(self):
        rep = report_of(WithOpenOrders())
        r = outcome(rep, rc=2)
        assert r["outcome"] == RO.MONITOR_FAILED
        assert "open carry order" in " ".join(r["reasons"])

    @pytest.mark.parametrize("mutate", [
        lambda r: r.update(venue="mainnet"),
        lambda r: r.update(use_testnet=False),
        lambda r: r.update(order_gate_would_allow_orders=True),
        lambda r: r.update(blockers=["something"]),
        lambda r: r.pop("entry_economics"),
        lambda r: r["entry_economics"].update(state="ENTRY_ECONOMICS_MAYBE"),
        lambda r: r.update(verdict="PREFLIGHT_WHATEVER"),
    ])
    def test_a_contradictory_report_fails(self, mutate):
        rep = report_of(Market(rate_bps=3.0))
        mutate(rep)
        assert outcome(rep)["outcome"] == RO.MONITOR_FAILED

    def test_the_exit_code_contradicting_the_verdict_fails(self):
        assert outcome(report_of(Market(rate_bps=3.0)), rc=1)["outcome"] \
            == RO.MONITOR_FAILED

    def test_a_refused_environment_fails(self):
        rep = {"verdict": "PREFLIGHT_REFUSED", "reason": "USE_TESTNET is not set"}
        assert outcome(rep, rc=3)["outcome"] == RO.MONITOR_FAILED

    @pytest.mark.parametrize("junk", [None, "x", [], {}, {"verdict": None}])
    def test_no_readable_report_fails(self, junk):
        assert outcome(junk)["outcome"] == RO.MONITOR_FAILED

    def test_the_monitor_never_names_conformance(self):
        for rep in (report_of(Market()), report_of(Market(rate_bps=0.3)), None):
            assert outcome(rep)["outcome"] in (
                RO.MONITOR_READY, RO.ENTRY_NOT_ELIGIBLE, RO.MONITOR_FAILED)

    def test_parse_report_finds_the_json_among_stderr_noise(self):
        rep = RF.preflight_report("PASS")
        text = "WARNING config x\n" + json.dumps(rep, indent=2) + "\ntrailing {not json\n"
        assert RO.parse_report(text)["verdict"] == "PREFLIGHT_OK"
        assert RO.parse_report("nothing here") is None


# -- the decision audit (what the report shows) --------------------------------

class TestTheDecisionIsAuditable:
    def test_it_exposes_the_inputs_and_what_each_one_is(self):
        d = report_of(Market(rate_bps=0.3))["entry_economics"]["decision"]
        assert d["schema"] == "entry_decision/1"
        assert d["fee_rates"]["perp_taker_bps"] == pytest.approx(5.5)
        assert d["execution_style"] == "taker"
        assert d["round_trip_bps_estimate"] == pytest.approx(11.0)
        f = d["funding"]
        assert f["smoothed_bps_per_8h_print"] == pytest.approx(0.3)
        assert f["next_settlement_utc_ms"] > 0 and f["minutes_to_next_settlement"] <= 480
        assert f["newest_print_age_hours"] == pytest.approx(2.0, abs=0.1)
        assert d["borrow"] == {"apr": 0.0, "bps_per_day": 0.0}
        assert d["basis_bps"] is not None
        h = d["hold_gate"]
        assert h["settlements_needed"] == 37 and h["settlements_budget"] == 17
        assert h["break_even_funding_bps_at_budget"] == pytest.approx(11 / 17)
        assert d["gate"]["state"] == "REFUSED"

    def test_eligibility_permission_and_profit_are_three_different_statements(self):
        d = report_of(Market(rate_bps=3.0))["entry_economics"]["decision"]
        i = d["interpretation"]
        assert d["gate"]["state"] == "PASS"
        assert "model output" in i["economic_eligibility"]
        assert "separate" in i["permission_to_execute"]
        assert "NOT established" in i["profitability"]
        assert "pnl_claim_allowed" in i["profitability"]
        assert "funding can turn" in i["profitability"]
        assert "not independent" in d["input_provenance"]["demonstrated_hold_note"]

    def test_the_fixed_constants_are_reported_unchanged(self):
        d = report_of(Market())["entry_economics"]["decision"]
        assert d["input_provenance"]["assumed_hold_days"] == 30.0
        assert d["input_provenance"]["demonstrated_hold_days"] == 5.8

    def test_acquire_mode_reports_the_spot_fee_too(self):
        d = report_of(Market(), mode="acquire")["entry_economics"]["decision"]
        assert d["fee_rates"]["spot_taker_bps"] == pytest.approx(10.0)
        assert d["round_trip_bps_estimate"] == pytest.approx(31.0)

    def test_the_audit_does_not_decide(self):
        """Same verdict whether or not the audit block is present."""
        rep = report_of(Market(rate_bps=0.3))
        econ = rep["entry_economics"]
        assert econ["state"] == "ENTRY_ECONOMICS_REFUSED"
        assert econ["decision"]["gate"]["reason"] == econ["reason"]


# -- one cycle, with a fake child runner ---------------------------------------

class FakeRunner:
    def __init__(self, report=None, rc=0, exc=None, raw=None):
        self.calls, self.report, self.rc, self.exc, self.raw = [], report, rc, exc, raw

    def __call__(self, argv, env, *, log_path, timeout_s=None):
        self.calls.append((list(argv), dict(env), timeout_s))
        if self.exc:
            raise self.exc
        text = self.raw if self.raw is not None else (
            json.dumps(self.report) if self.report is not None else "")
        with open(log_path, "w") as fh:
            fh.write(text)
        return self.rc


def cycle(tmp_path, runner, *, probe=200):
    code = RM.monitor_once(str(tmp_path / "run"), env=ENV, prober=lambda: probe,
                           runner=runner, sleep=lambda s: None,
                           history_root=str(tmp_path))
    return code, json.load(open(tmp_path / "run" / "monitor.json"))


class TestOneMonitorCycle:
    def test_ready_and_not_eligible_both_exit_zero_and_failed_does_not(self, tmp_path):
        assert cycle(tmp_path, FakeRunner(RF.preflight_report("PASS")))[0] == 0
        c, rec = cycle(tmp_path, FakeRunner(RF.preflight_report("REFUSED")))
        assert c == 0 and rec["outcome"] == RO.ENTRY_NOT_ELIGIBLE
        c, rec = cycle(tmp_path, FakeRunner(RF.preflight_report(
            "PASS", verdict="PREFLIGHT_BLOCKED", blockers=["x"]), rc=2))
        assert c == RM.EXIT_FAILED and rec["outcome"] == RO.MONITOR_FAILED

    @pytest.mark.parametrize("status", [403, 0, 500])
    def test_a_network_failure_fails_and_runs_no_child(self, tmp_path, status):
        runner = FakeRunner(RF.preflight_report("PASS"))
        code, rec = cycle(tmp_path, runner, probe=status)
        assert code == RM.EXIT_UNREACHABLE and rec["outcome"] == RO.MONITOR_FAILED
        assert runner.calls == []
        assert "not reachable" in rec["reasons"][0]

    @pytest.mark.parametrize("exc", [OSError("cannot start"), RuntimeError("x"),
                                     TimeoutError("t")])
    def test_a_child_that_cannot_run_is_a_failure_not_a_crash(self, tmp_path, exc):
        code, rec = cycle(tmp_path, FakeRunner(exc=exc))
        assert code == RM.EXIT_FAILED and rec["outcome"] == RO.MONITOR_FAILED

    def test_a_timeout_or_empty_output_is_a_failure(self, tmp_path):
        assert cycle(tmp_path, FakeRunner(rc=124))[1]["outcome"] == RO.MONITOR_FAILED
        assert cycle(tmp_path, FakeRunner(raw="garbage"))[1]["outcome"] == RO.MONITOR_FAILED

    def test_a_shutdown_propagates(self, tmp_path):
        with pytest.raises(UD.Shutdown):
            cycle(tmp_path, FakeRunner(exc=UD.Shutdown("signal 15")))

    def test_only_the_read_only_preflight_is_ever_started(self, tmp_path):
        runner = FakeRunner(RF.preflight_report("PASS"))
        cycle(tmp_path, runner)
        assert len(runner.calls) == 1
        argv, env, timeout = runner.calls[0]
        assert [os.path.basename(a) for a in argv[1:]] == ["carry_preflight.py"]
        assert timeout == RM.PREFLIGHT_TIMEOUT_S

    def test_the_child_environment_closes_every_order_gate(self, tmp_path):
        runner = FakeRunner(RF.preflight_report("PASS"))
        cycle(tmp_path, runner)
        env = runner.calls[0][1]
        assert env["PAPER_TRADING"] == "1"           # carry_orders_permitted refuses
        assert env["USE_TESTNET"] == "1" and env["BYBIT_VENUE"] == "testnet"
        assert env["BOOK_MODE"] == "carry"
        assert "LIVE_TRADING_ACK" not in env
        assert not os.path.exists(env["STATE_DB_PATH"])
        assert env["STATE_DB_PATH"].startswith(str(tmp_path / "run"))

    def test_a_report_that_says_orders_are_allowed_is_a_failure(self, tmp_path):
        code, rec = cycle(tmp_path, FakeRunner(
            RF.preflight_report("PASS", order_gate_open=True)))
        assert rec["outcome"] == RO.MONITOR_FAILED
        assert "ALLOW orders" in " ".join(rec["reasons"])

    def test_the_record_and_the_history_line(self, tmp_path):
        cycle(tmp_path, FakeRunner(RF.preflight_report("REFUSED")))
        rec = json.load(open(tmp_path / "run" / "monitor.json"))
        assert rec["schema"] == "monitor/1" and rec["order_sent"] is False
        assert rec["outcome"] == RO.ENTRY_NOT_ELIGIBLE
        lines = [json.loads(l) for l in open(tmp_path / "monitor.jsonl")]
        assert len(lines) == 1 and lines[0]["order_sent"] is False
        assert lines[0]["outcome"] == RO.ENTRY_NOT_ELIGIBLE
        assert lines[0]["gate_reason"] == "BREAKEVEN_HOLD_EXCEEDS_DEMONSTRATED_HOLD"
        assert lines[0]["settlements_needed"] == 11 and lines[0]["round_trip_bps"] == 11.0

    def test_credentials_never_reach_the_record(self, tmp_path):
        env = dict(ENV, BYBIT_API_KEY="KEYVALUE123", BYBIT_API_SECRET="SECRETVALUE456")
        RM.monitor_once(str(tmp_path / "run"), env=env, prober=lambda: 200,
                        runner=FakeRunner(RF.preflight_report("PASS")),
                        sleep=lambda s: None)
        blob = "".join(open(os.path.join(tmp_path / "run", f)).read()
                       for f in os.listdir(tmp_path / "run")
                       if os.path.isfile(os.path.join(tmp_path / "run", f)))
        assert "KEYVALUE123" not in blob and "SECRETVALUE456" not in blob


class TestTheLoop:
    def test_it_runs_once_per_interval_and_stops_on_the_stop_file(self, tmp_path):
        ticks = {"n": 0}

        def once(run_dir, *, env, history_root):
            ticks["n"] += 1
            if ticks["n"] == 3:
                open(tmp_path / "STOP", "w").close()
            return 0
        sleeps = []
        code = RM.loop(str(tmp_path), env=ENV, interval_s=3600, once=once,
                       sleep=sleeps.append, clock=lambda: 1_800_000_000.0)
        assert code == RM.EXIT_OK and ticks["n"] == 3
        assert sum(sleeps) == pytest.approx(2 * 3600)

    def test_a_shutdown_ends_the_loop_with_143(self, tmp_path):
        def once(*a, **k):
            raise UD.Shutdown("signal 15")
        assert RM.loop(str(tmp_path), env=ENV, interval_s=3600, once=once,
                       sleep=lambda s: None) == RM.EXIT_SIGNALLED


class TestMain:
    def test_missing_credentials_are_a_refusal_naming_only_the_variable(
            self, monkeypatch, capsys):
        monkeypatch.delenv("BYBIT_API_SECRET", raising=False)
        monkeypatch.setenv("BYBIT_API_KEY", "sekrit-value")
        assert RM.main(["--run-dir", "/tmp/x", "--once"]) == RM.EXIT_REFUSED
        err = capsys.readouterr().err
        assert "BYBIT_API_SECRET" in err and "sekrit-value" not in err

    def test_a_mode_is_required(self):
        with pytest.raises(SystemExit):
            RM.main(["--run-dir", "/tmp/x"])

    def test_a_too_short_interval_is_refused(self, monkeypatch):
        monkeypatch.setenv("BYBIT_API_KEY", "k")
        monkeypatch.setenv("BYBIT_API_SECRET", "s")
        assert RM.main(["--run-dir", "/tmp/x", "--loop",
                        "--interval-minutes", "0.1"]) == RM.EXIT_REFUSED


# -- structurally read-only -----------------------------------------------------

class TestTheMonitorCannotTrade:
    SRC = os.path.join(BOT, "tools", "runner_monitor.py")

    def test_it_references_no_order_capable_tool_or_flag(self):
        text = open(self.SRC, encoding="utf-8").read()
        code = "\n".join(l for l in text.splitlines()
                         if not l.lstrip().startswith("#"))
        tree = ast.parse(text)
        strings = [n.value for n in ast.walk(tree)
                   if isinstance(n, ast.Constant) and isinstance(n.value, str)
                   and n is not None]
        # docstrings explain what is NOT started; executable strings must not name it
        executable = [x for x in strings if "\n" not in x]
        for forbidden in ("drill.py", "close_testnet_short", "--arm",
                          "place_market", "place_post_only", "cancel_order"):
            assert not any(forbidden in x for x in executable), forbidden
        assert "carry_preflight.py" in executable

    def test_it_imports_no_order_capable_module(self):
        tree = ast.parse(open(self.SRC, encoding="utf-8").read())
        names = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                names.update(a.name.split(".")[0] for a in n.names)
            elif isinstance(n, ast.ImportFrom) and n.module:
                names.add(n.module.split(".")[0])
        assert names <= {"argparse", "json", "os", "sys", "time", "typing",
                         "__future__", "run_outcomes", "unattended_drill"}

    def test_a_full_preflight_flow_sends_no_non_get_request(self):
        for market in (Market(), Market(rate_bps=0.3), Market(newest_age_h=20.0),
                       Market(perp_qty=0.004), WithOpenOrders(),
                       Market(fee_fails=True)):
            market.non_gets = getattr(market, "non_gets", [])
            report_of(market)
            assert market.non_gets == []

    def test_the_broker_the_preflight_builds_refuses_every_order(self):
        from carry_broker import CarryOrderRefused
        broker = CarryBroker(client=Market(), sequence_source=lambda *a: 0,
                             order_gate=lambda: (False, "preflight is read-only"))
        with pytest.raises(CarryOrderRefused):
            broker.place_market(symbol="BTCUSDT", side="Sell", qty=0.001,
                                product="linear")


# -- review findings -----------------------------------------------------------------

class TestReviewFindings:
    def test_a_stop_signal_in_the_sleep_between_cycles_ends_the_loop_with_143(
            self, tmp_path):
        """The loop spends nearly all its time asleep: a SIGTERM there used to
        escape as a traceback and exit 1, which the Fly on-failure policy counts
        as a failure."""
        def once(*a, **k):
            return 0

        def sleeper(s):
            raise UD.Shutdown("signal 15")
        assert RM.loop(str(tmp_path), env=ENV, interval_s=3600, once=once,
                       sleep=sleeper) == RM.EXIT_SIGNALLED

    def test_the_real_main_turns_a_sigterm_into_143_not_a_traceback(
            self, tmp_path):
        import signal
        import subprocess
        import textwrap
        driver = tmp_path / "d.py"
        driver.write_text(textwrap.dedent(f"""
            import os, signal, sys, time, threading
            sys.path.insert(0, {os.path.join(BOT, "tools")!r})
            import runner_monitor as RM, unattended_drill as UD
            RM.monitor_once = lambda run_dir, **k: 0
            def fire():
                time.sleep(1.5); os.kill(os.getpid(), signal.SIGTERM)
            threading.Thread(target=fire, daemon=True).start()
            sys.exit(RM.main(["--run-dir", {str(tmp_path / "root")!r}, "--loop",
                              "--interval-minutes", "60"]))
        """))
        env = dict(os.environ, BYBIT_API_KEY="k", BYBIT_API_SECRET="s")
        done = subprocess.run([sys.executable, str(driver)], env=env,
                              capture_output=True, text=True, timeout=60)
        assert done.returncode == RM.EXIT_SIGNALLED, done.stderr
        assert "Traceback" not in done.stderr

    def test_pathological_output_is_a_failure_not_a_crash(self, tmp_path):
        deep = '{"a":' * 100_000
        assert RO.parse_report(deep) is None
        code, rec = cycle(tmp_path, FakeRunner(raw=deep))
        assert rec["outcome"] == RO.MONITOR_FAILED

    def test_a_blockers_value_that_is_not_a_list_is_a_failure_not_a_traceback(self):
        r = RO.classify_preflight({"verdict": "PREFLIGHT_BLOCKED",
                                   "blockers": None}, 2)
        assert r["outcome"] == RO.MONITOR_FAILED
        r = RO.classify_preflight({"verdict": "PREFLIGHT_OK", "blockers": 5,
                                   "venue": "testnet", "use_testnet": True}, 0)
        assert r["outcome"] == RO.MONITOR_FAILED

    def test_a_non_mapping_decision_does_not_break_the_history_line(self, tmp_path):
        rep = RF.preflight_report("PASS")
        rep["entry_economics"]["decision"] = "not a mapping"
        code, rec = cycle(tmp_path, FakeRunner(rep))
        assert rec["outcome"] == RO.MONITOR_READY
        assert (tmp_path / "monitor.jsonl").exists()

    def test_a_second_object_after_the_report_cannot_outvote_it(self):
        real = RF.preflight_report("REFUSED")
        forged = RF.preflight_report("PASS")
        text = json.dumps(real, indent=2) + "\nWARNING upstream said\n" \
            + json.dumps(forged)
        assert RO.parse_report(text) is None            # ambiguous: no verdict
        inline = json.dumps(real, indent=2) + "\nnote: " + json.dumps(forged)
        assert RO.parse_report(inline)["entry_economics"]["state"] == \
            "ENTRY_ECONOMICS_REFUSED"                   # not at a line start
        assert RO.classify_preflight(RO.parse_report(text), 0)["outcome"] \
            == RO.MONITOR_FAILED

    def test_the_economic_refusals_are_exactly_the_gates_economic_reasons(self):
        gate = set(carry_costs.GATE_REASONS) - {carry_costs.GATE_REASONS[0]}
        assert RO.ECONOMIC_REFUSALS | (RO.INPUT_REASONS & gate) == gate
        assert not (RO.ECONOMIC_REFUSALS & RO.INPUT_REASONS)

    def test_a_refusal_with_an_empty_or_unknown_reason_fails_closed(self):
        for reason in ("", None, "SOMETHING_NEW"):
            rep = RF.preflight_report("REFUSED", reason=reason or "")
            if reason is None:
                rep["entry_economics"].pop("reason")
            assert RO.classify_preflight(rep, 0)["outcome"] == RO.MONITOR_FAILED

    def test_the_monitor_uses_only_helpers_from_the_conformance_module(self):
        """It imports unattended_drill for plumbing (probe, child runner, lock,
        prune, signals). It may use no more than that."""
        tree = ast.parse(open(TestTheMonitorCannotTrade.SRC, encoding="utf-8").read())
        used = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
                and isinstance(n.value, ast.Name) and n.value.id == "UD"}
        assert used <= {"probe", "run_child", "Shutdown", "install_signal_handlers",
                        "acquire_lock", "prune_runs", "_say", "PROBE_TRIES",
                        "PROBE_PAUSE_S", "TOOLS", "CREDENTIALS", "EXIT_SIGNALLED"}, used


class TestTheReconciliationAndDrillReaders:
    GOOD = {"schema": "evidence_reconciliation/1",
            "carry_cycle": [{"provenance": "VENUE_RECONCILED", "resolved": True}]}

    def test_only_a_fully_reconciled_resolved_cycle_counts(self):
        assert RO.cycle_reconciled(self.GOOD) is True
        two = dict(self.GOOD, carry_cycle=[
            {"provenance": "VENUE_RECONCILED", "resolved": True},
            {"provenance": "LOCAL_RECORD", "resolved": True}])
        assert RO.cycle_reconciled(two) is False                    # all, not any
        for bad in (dict(self.GOOD, carry_cycle=[]),                # none captured
                    {k: v for k, v in self.GOOD.items() if k != "carry_cycle"},
                    dict(self.GOOD, carry_cycle=[{"provenance": "VENUE_RECONCILED",
                                                  "resolved": False}]),
                    dict(self.GOOD, carry_cycle=[{"provenance": "VENUE_RECONCILED"}]),
                    dict(self.GOOD, schema="other/1"), {}, None, [], "x"):
            assert RO.cycle_reconciled(bad) is False

    def test_a_stand_aside_must_be_a_failed_stage_with_the_stand_aside_action(self):
        def rep(**ev):
            stage = {"stage": "open", "ok": False, "evidence": {
                "action": "stand_aside",
                "reason": "BREAKEVEN_HOLD_EXCEEDS_DEMONSTRATED_HOLD"}}
            stage.update({k: v for k, v in ev.items() if k != "evidence"})
            stage["evidence"] = {**stage["evidence"], **ev.get("evidence", {})}
            return {"orders_sent": 0, "stages": [stage]}
        assert RO.stood_aside_without_orders(rep()) == \
            "BREAKEVEN_HOLD_EXCEEDS_DEMONSTRATED_HOLD"
        assert RO.stood_aside_without_orders(rep(ok=True)) is None
        assert RO.stood_aside_without_orders(
            rep(evidence={"action": "opened"})) is None
        assert RO.stood_aside_without_orders(
            dict(rep(), orders_sent=1)) is None
        assert RO.stood_aside_without_orders(
            {k: v for k, v in rep().items() if k != "orders_sent"}) is None
        assert RO.stood_aside_without_orders(None) is None

    def test_orders_exercised_needs_an_entry_and_an_exit_and_nothing_open(self):
        assert RO.orders_exercised({"orders_sent": 2}) is True
        for bad in ({"orders_sent": 1}, {"orders_sent": 0}, {"orders_sent": True},
                    {"orders_sent": 2, "still_open": True}, {}, None, []):
            assert RO.orders_exercised(bad) is False
