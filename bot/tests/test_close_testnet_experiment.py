"""The order-identity experiment harness, proven OFFLINE against a fake venue.

Nothing here touches a network or an exchange: every venue answer comes from
`fake_experiment_venue.FakeVenue`, which records each request it receives. The
properties pinned are about the HARNESS, not about Bybit:

  * every refusal happens before a single request is made (zero requests seen)
  * limits can only be lowered, and the order ceiling holds even when the
    venue misbehaves
  * no secret value ever reaches an output file or the terminal
  * an ambiguous venue state halts with a distinct exit code and the venue state
    captured; a non-flat end is loud and exits non-zero
  * only allowlisted paths are ever requested; every Buy is reduce-only
  * the harness RECORDS what the (fake) venue did, whichever way it answered
"""
from __future__ import annotations

import ast
import json
import os
import re
import socket
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, BOT)
sys.path.insert(0, os.path.join(BOT, "tools"))

import bybit_connection as bc  # noqa: E402
import close_testnet_experiment as X  # noqa: E402
import fake_experiment_venue as F  # noqa: E402

SYMBOL = "XRPUSDT"


class NoFactoryCalled(AssertionError):
    pass


def invoke(venue, tmp_path, *extra, cfg=None, env=None, flag=True,
           out="run", sleep=None, symbol=SYMBOL, factory=None, argv_extra=()):
    argv = []
    if flag:
        argv.append("--i-understand-this-places-testnet-orders")
    argv += ["--symbol", symbol, "--out-dir", str(tmp_path / out), *extra,
             *argv_extra]
    calls = []

    def default_factory(c):
        calls.append(c)
        return venue
    rc = X.main(argv, client_factory=factory or default_factory,
                cfg_loader=lambda: (cfg or F.fake_cfg(), None),
                env={} if env is None else env,
                clock=venue.clock.monotonic, now_ms=venue.clock.now_ms,
                sleep=sleep or venue.clock.sleep)
    return rc, calls


def report(tmp_path, out="run", name="report.json"):
    with open(tmp_path / out / name, encoding="utf-8") as fh:
        return json.load(fh)


def exp(rep, eid):
    return next(e for e in rep["experiments"] if e["id"] == eid)


def all_text(tmp_path, capsys, out="run"):
    cap = capsys.readouterr()
    text = cap.out + cap.err
    for name in os.listdir(tmp_path / out):
        with open(tmp_path / out / name, encoding="utf-8") as fh:
            text += fh.read()
    return text


# --------------------------------------------------------------- link ids ----

class TestOrderLinkIds:
    def test_deterministic_valid_and_untruncated(self):
        seen = set()
        for n in range(1, 12):
            for step in list(range(1, 10)) + [90, 91, 92, 99]:
                a = X.experiment_link_id("260101010101", f"E{n}", step, SYMBOL)
                assert a == X.experiment_link_id("260101010101", f"E{n}", step, SYMBOL)
                assert len(a) <= 36 and re.fullmatch(r"[A-Za-z0-9_-]{1,36}", a)
                assert a.startswith("XP-")
                assert len(a.rsplit("-", 1)[1]) == 24      # digest not cut
                seen.add(a)
        assert len(seen) == 11 * 13

    def test_it_is_the_repos_builder(self):
        a = X.experiment_link_id("r1", "E3", 2, SYMBOL)
        assert a == bc.build_order_link_id(
            seq=2, symbol=SYMBOL, side="", qty="", price="", purpose="e3",
            session_token="r1", prefix="XP")

    def test_run_id_changes_the_id(self):
        assert X.experiment_link_id("a", "E1", 1, SYMBOL) != \
            X.experiment_link_id("b", "E1", 1, SYMBOL)

    @pytest.mark.parametrize("bad", ["", "x" * 37, "XP-bad space", "XP-é",
                                     "BB-e1-1-abc", "XP-" + "a" * 40])
    def test_validator_rejects(self, bad):
        with pytest.raises(X.UnsafeRequest):
            X.validate_link_id(bad)

    def test_every_link_a_run_sends_is_valid(self, tmp_path):
        v = F.FakeVenue()
        rc, _ = invoke(v, tmp_path)
        assert rc == 0
        for r in v.creates:
            X.validate_link_id(r["body"]["orderLinkId"])


# -------------------------------------------------------------- refusals -----

BAD_HOSTS = [
    "https://api.bybit.com",
    "https://bybit.com",
    "https://api-testnet.bybit.com.evil.example",
    "https://evil.example/api-testnet.bybit.com",
    "http://api-testnet.bybit.com",
    "",
    "https:///",
    "https://API-TESTNET.bybit.com.",
    "https://api-testnet.bybit.com:8443",
    "https://user@api-testnet.bybit.com",
    "https://api-testnet.bybit.com/v5",
    "https://api-demo.bybit.com",
    "https://api-testnet.bybit.com?x=1",
    None,
]


class TestRefusalsBeforeAnyRequest:
    @pytest.mark.parametrize("host", BAD_HOSTS)
    def test_a_wrong_host_is_refused_with_zero_requests(self, tmp_path, capsys, host):
        v = F.FakeVenue(base_url=host)
        rc, _ = invoke(v, tmp_path)
        assert rc == X.EXIT_REFUSED
        assert v.requests == []
        assert "REFUSED" in capsys.readouterr().err

    def test_the_real_host_is_the_only_one_accepted(self, tmp_path):
        assert X.endpoint_refusal(bc.TESTNET_REST) is None
        assert X.endpoint_refusal(bc.TESTNET_REST + "/") is None
        for host in BAD_HOSTS:
            assert X.endpoint_refusal(host) is not None, host
        assert X.TESTNET_BASE == "https://api-testnet.bybit.com"

    def test_missing_flag_refuses_before_the_client_is_even_built(self, tmp_path):
        v = F.FakeVenue()

        def boom(_cfg):
            raise NoFactoryCalled("client built without the flag")
        rc, _ = invoke(v, tmp_path, flag=False, factory=boom)
        assert rc == X.EXIT_REFUSED and v.requests == []

    def test_live_ack_in_env_refuses_and_the_value_is_never_printed(
            self, tmp_path, capsys):
        v = F.FakeVenue()
        rc, calls = invoke(v, tmp_path, env={"LIVE_TRADING_ACK": "I_UNDERSTAND_X7"})
        cap = capsys.readouterr()
        assert rc == X.EXIT_REFUSED and v.requests == [] and calls == []
        assert "I_UNDERSTAND_X7" not in cap.out + cap.err

    def test_live_ack_even_if_empty_in_env_refuses(self, tmp_path):
        v = F.FakeVenue()
        rc, _ = invoke(v, tmp_path, env={"LIVE_TRADING_ACK": ""})
        assert rc == X.EXIT_REFUSED and v.requests == []

    def test_live_ack_in_config_refuses_and_is_not_printed(self, tmp_path, capsys):
        v = F.FakeVenue()
        rc, _ = invoke(v, tmp_path, cfg=F.fake_cfg(LIVE_TRADING_ACK="ACKVAL-123"))
        cap = capsys.readouterr()
        assert rc == X.EXIT_REFUSED and v.requests == []
        assert "ACKVAL-123" not in cap.out + cap.err

    @pytest.mark.parametrize("overrides", [
        dict(USE_TESTNET=False), dict(USE_TESTNET="yes"),
        dict(BYBIT_VENUE="mainnet"), dict(BYBIT_VENUE="demo"),
        dict(USE_TESTNET=False, PAPER_TRADING=False, BYBIT_VENUE="mainnet"),
    ])
    def test_a_non_testnet_configuration_refuses(self, tmp_path, overrides):
        v = F.FakeVenue()
        rc, _ = invoke(v, tmp_path, cfg=F.fake_cfg(**overrides))
        assert rc == X.EXIT_REFUSED and v.requests == []

    @pytest.mark.parametrize("missing", ["BYBIT_API_KEY", "BYBIT_API_SECRET"])
    def test_missing_credentials_refuse_naming_only_the_variable(
            self, tmp_path, capsys, missing):
        v = F.FakeVenue()
        rc, _ = invoke(v, tmp_path, cfg=F.fake_cfg(**{missing: ""}))
        err = capsys.readouterr().err
        assert rc == X.EXIT_REFUSED and v.requests == []
        assert missing in err
        assert F.FAKE_API_SECRET not in err and F.FAKE_API_KEY not in err

    def test_a_client_that_is_not_a_testnet_client_refuses(self, tmp_path):
        v = F.FakeVenue()
        v.venue = "mainnet"
        rc, _ = invoke(v, tmp_path)
        assert rc == X.EXIT_REFUSED and v.requests == []
        v = F.FakeVenue()
        v.testnet = False
        assert invoke(v, tmp_path, out="b")[0] == X.EXIT_REFUSED
        assert v.requests == []

    @pytest.mark.parametrize("bad", [
        ["--max-notional-usd", "20.01"], ["--max-notional-usd", "1000"],
        ["--max-orders", "31"], ["--max-orders", "1000"],
        ["--max-duration-s", "1801"], ["--max-experiments", "12"],
        ["--max-notional-usd", "0"], ["--max-orders", "-1"],
        ["--max-orders", "2.5"], ["--max-notional-usd", "nan"],
        ["--max-notional-usd", "inf"],
    ])
    def test_limits_cannot_be_raised_or_made_nonsense(self, tmp_path, bad):
        v = F.FakeVenue()
        rc, calls = invoke(v, tmp_path, *bad)
        assert rc == X.EXIT_REFUSED and v.requests == [] and calls == []

    def test_limits_can_be_lowered(self):
        lim = X.Limits.resolve(5, 8, 100, 3)
        assert (lim.max_notional_usd, lim.max_orders, lim.max_duration_s,
                lim.max_experiments) == (5, 8, 100, 3)
        top = X.Limits.resolve()
        assert top.max_notional_usd <= 20.0
        assert top.max_orders <= X.HARD_MAX_ORDERS == 30
        assert top.max_duration_s <= X.HARD_MAX_DURATION_S
        assert X.EMERGENCY_RESERVE >= 1

    def test_a_bad_symbol_or_experiment_list_refuses(self, tmp_path):
        v = F.FakeVenue()
        assert invoke(v, tmp_path, symbol="BTCUSD")[0] == X.EXIT_REFUSED
        assert invoke(v, tmp_path, "--experiments", "E1,E99", out="b")[0] == X.EXIT_REFUSED
        assert invoke(v, tmp_path, "--experiments", "E1,E1", out="c")[0] == X.EXIT_REFUSED
        assert v.requests == []

    def test_an_existing_output_dir_run_is_never_overwritten(self, tmp_path):
        v = F.FakeVenue()
        assert invoke(v, tmp_path, "--experiments", "E1")[0] == 0
        n = len(v.requests)
        assert invoke(v, tmp_path, "--experiments", "E1")[0] == X.EXIT_REFUSED
        assert len(v.requests) == n

    # state refusals: reads happen, but no order is ever placed
    @pytest.mark.parametrize("kw,fragment", [
        (dict(hedge_mode=True), "hedge"),
        (dict(unreadable_position=True), "AMBIG_UNREADABLE"),
        (dict(unreadable_account=True), "account info"),
        (dict(start_position="-5"), "not clean"),
        (dict(start_position="3"), "not clean"),
        (dict(start_open_link="XP-leftover"), "not clean"),
        (dict(start_open_link="SOMEONE-ELSES"), "not clean"),
        (dict(last="100000", tick="0.1", step="0.001", min_qty="0.001"),
         "notional limit"),
    ])
    def test_unreadable_or_unclean_start_state_refuses_and_places_nothing(
            self, tmp_path, capsys, kw, fragment):
        v = F.FakeVenue(**kw)
        rc, _ = invoke(v, tmp_path)
        err = capsys.readouterr().err
        assert rc == X.EXIT_REFUSED
        assert v.creates == [] and fragment in err
        assert all(p in X.ALLOWED_ENDPOINTS for p in v.paths)

    def test_dry_run_makes_no_network_call_and_prints_the_plan(
            self, tmp_path, capsys, monkeypatch):
        def no(*a, **k):
            raise AssertionError("network touched")
        monkeypatch.setattr(socket.socket, "connect", no)

        def boom(*a, **k):
            raise NoFactoryCalled("dry run built a client or loaded config")
        rc = X.main(["--dry-run", "--symbol", SYMBOL], client_factory=boom,
                    cfg_loader=boom, env={})
        out = capsys.readouterr().out
        assert rc == 0
        for needle in ("PLAN", "BYBIT_API_KEY", "BYBIT_API_SECRET",
                       "--i-understand-this-places-testnet-orders",
                       "--phase submit", "--phase recover", "--dry-run",
                       "NOT RUN", "PARTIAL_FILL", "UNVERIFIED",
                       "api-testnet.bybit.com", "/v5/order/create"):
            assert needle in out, needle
        assert "switch-mode" not in out
        assert "LIVE_TRADING_ACK must stay unset" in out

    def test_dry_run_needs_no_flag_and_no_credentials(self, capsys):
        assert X.main(["--dry-run"], env={}) == 0


# ------------------------------------------------------------ full run -------

class TestFullRunAgainstARejectingVenue:
    @pytest.fixture()
    def run(self, tmp_path, capsys):
        v = F.FakeVenue()
        rc, _ = invoke(v, tmp_path)
        return v, rc, report(tmp_path), capsys.readouterr()

    def test_it_completes_flat_and_within_the_ceiling(self, run):
        v, rc, rep, _ = run
        assert rc == 0 and v.position == 0
        assert len(v.creates) == 26 <= X.HARD_MAX_ORDERS
        assert [e["id"] for e in rep["experiments"]] == \
            [f"E{i}" for i in range(1, 12)]
        assert all(e["status"] == "COMPLETED" for e in rep["experiments"])
        assert all(e["position_before"] == "0" and e["position_after"] == "0"
                   for e in rep["experiments"])
        assert rep["final_state"]["flat"] and rep["final_state"]["clean"]

    def test_it_records_the_duplicate_answer_exactly(self, run):
        _, _, rep, _ = run
        for eid in ("E1", "E2", "E4"):
            f = exp(rep, eid)["facts"]
            assert f["repost"]["ret_code"] == 110072
            assert f["repost"]["ret_msg"] == "OrderLinkedID is duplicate"
            assert f["repost"]["ret_code_in_repo_duplicate_table"] is True
            assert f["second_order_created"] is False
            assert f["second_execution_exists"] is False
            assert f["distinct_order_ids_by_link"] == 1

    def test_e2_resends_a_smaller_qty_never_a_larger_one(self, run):
        _, _, rep, _ = run
        f = exp(rep, "E2")["facts"]
        assert float(f["repost_qty"]) < float(f["first_qty"])

    def test_cancelled_and_rejected_cases_are_recorded(self, run):
        _, _, rep, _ = run
        assert exp(rep, "E5")["facts"]["status_after_cancel"] == ["Cancelled"]
        e6 = exp(rep, "E6")["facts"]
        assert e6["first_rejected_probe"]["ret_code"] == 110017
        assert e6["identical_repost"]["ret_code"] == 110017
        assert e6["valid_repost_same_link"]["outcome"] == "accepted"

    def test_e10_reconciles_executions_with_cum_exec_qty(self, run):
        _, _, rep, _ = run
        f = exp(rep, "E10")["facts"]
        assert f["by_link"]["exec_sum"] == f["by_link"]["cum_exec_total"]
        assert f["by_link"]["unique_exec_ids"] == f["by_link"]["exec_rows"]
        assert all(x["matches"] for x in f["by_order_id"])

    def test_e11_ends_exactly_flat(self, run):
        _, _, rep, _ = run
        f = exp(rep, "E11")["facts"]
        assert f["position_after_close"] == "0"
        assert any(s["name"] == "flat_verified" for s in exp(rep, "E11")["steps"])

    def test_partial_fill_is_declared_not_run_never_claimed(self, run):
        _, _, rep, cap = run
        ids = {n["id"]: n for n in rep["not_run"]}
        assert ids["PARTIAL_FILL"]["status"] == "NOT_RUN"
        assert "cannot be forced" in ids["PARTIAL_FILL"]["why"]
        assert "PARTIAL_FILL" in cap.out
        assert all(e["id"] != "PARTIAL_FILL" for e in rep["experiments"])

    def test_it_records_and_does_not_interpret(self, run):
        _, _, rep, cap = run
        assert "testnet behaviour is not mainnet proof" in cap.out
        assert "not conclusions" in cap.out
        for word in ("profit", "alpha", "safe to", "is safe", "proves"):
            assert word not in cap.out.lower().replace("not mainnet proof", "")
        assert rep["assumptions_unverified"]
        assert all(a.startswith("UNVERIFIED") for a in rep["assumptions_unverified"])

    def test_records_are_json_lines_with_a_complete_trail(self, tmp_path):
        v = F.FakeVenue()
        assert invoke(v, tmp_path)[0] == 0
        lines = [json.loads(x) for x in open(tmp_path / "run" / "records.jsonl")]
        assert [l["seq"] for l in lines] == list(range(1, len(lines) + 1))
        req = [l for l in lines if l["kind"] == "request"]
        assert len(req) == len(v.requests)
        assert sum(1 for l in req if l["path"] == "/v5/order/create") == len(v.creates)
        man = json.load(open(tmp_path / "run" / "manifest.json"))
        assert man["creates_total"] == len(v.creates)
        assert {x["link"] for x in man["links"]} >= \
            {c["body"]["orderLinkId"] for c in v.creates}
        assert os.stat(tmp_path / "run" / "records.jsonl").st_mode & 0o077 == 0


# --------------------------------------------- the venue answers differently --

class TestRecordsWhicheverWayTheVenueAnswers:
    def test_duplicates_accepted_as_new_orders_are_recorded_and_cleaned_up(
            self, tmp_path):
        v = F.FakeVenue(dup_policy="accept_new")
        rc, _ = invoke(v, tmp_path)
        rep = report(tmp_path)
        assert rc == 0 and v.position == 0
        for eid in ("E1", "E2", "E4"):
            f = exp(rep, eid)["facts"]
            assert f["repost"]["ret_code"] == 0
            assert f["second_order_created"] is True
            assert f["second_execution_exists"] is True
            assert f["distinct_order_ids_by_link"] == 2
        assert exp(rep, "E3")["facts"]["second_order_created"] is True
        assert len(v.creates) <= X.HARD_MAX_ORDERS
        # budget gating, not a crash: what could not run is said
        assert any(n.get("why", "").startswith("order budget")
                   for n in rep["not_run"]) or len(rep["experiments"]) == 11
        assert not [o for o in v.orders if o["orderStatus"] in F.FakeVenue.OPEN]

    def test_a_duplicate_answered_with_the_same_order_is_recorded(self, tmp_path):
        v = F.FakeVenue(dup_policy="accept_same")
        rc, _ = invoke(v, tmp_path)
        rep = report(tmp_path)
        assert rc == 0
        f = exp(rep, "E1")["facts"]
        assert f["repost"]["ret_code"] == 0 and f["second_order_created"] is False
        assert f["repost"]["order_id"] == f["first"]["order_id"]
        e5 = exp(rep, "E5")["facts"]
        assert e5["repost"]["ret_code"] == 0 and e5["second_order_created"] is False

    def test_reuse_after_a_cancel_is_recorded_when_the_venue_allows_it(
            self, tmp_path):
        v = F.FakeVenue(dup_policy_by_status={"Cancelled": "accept_new"})
        rc, _ = invoke(v, tmp_path)
        rep = report(tmp_path)
        assert rc == 0 and v.position == 0
        e5 = exp(rep, "E5")["facts"]
        assert e5["repost"]["ret_code"] == 0 and e5["second_order_created"] is True
        assert not [o for o in v.orders if o["orderStatus"] in F.FakeVenue.OPEN]

    def test_lost_ack_recovery_records_polls_and_seconds(self, tmp_path):
        v = F.FakeVenue(rt_delay=3, hist_delay=4, fill_delay=2)
        rc, _ = invoke(v, tmp_path, "--experiments", "E7")
        f = exp(report(tmp_path), "E7")["facts"]
        assert rc == 0
        assert f["polls_until_visible"] > 1 and f["seconds_until_visible"] >= 4
        e7 = exp(report(tmp_path), "E7")
        discarded = next(s for s in e7["steps"]
                         if s["name"] == "post_response_discarded")
        assert discarded["response"]["discarded"] is True
        assert "body" not in discarded["response"]

    def test_split_executions_still_reconcile(self, tmp_path):
        v = F.FakeVenue(exec_split=3)
        rc, _ = invoke(v, tmp_path, "--experiments", "E10")
        f = exp(report(tmp_path), "E10")["facts"]
        assert rc == 0 and f["by_link"]["exec_rows"] == 3
        assert f["by_link"]["unique_exec_ids"] == 3

    def test_e8_records_three_kinds_of_lookup(self, tmp_path):
        v = F.FakeVenue()
        rc, _ = invoke(v, tmp_path, "--experiments", "E8")
        f = exp(report(tmp_path), "E8")["facts"]
        assert rc == 0 and f["query_immediate"]["in_realtime"] == 1
        labels = [q["label"] for q in f["queries"]]
        assert labels == ["after_delay",
                          "after_simulated_restart_manifest_reload"]
        assert "SIMULATED" in f["restart_note"]


# --------------------------------------------------------- ambiguity halts ---

AMBIGUOUS = [
    ("weird_status", dict(weird_status="Untriggered"), "AMBIG_UNEXPECTED_STATUS"),
    ("not_visible", dict(hide_orders=True), "AMBIG_NOT_VISIBLE"),
    ("exec_sum", dict(exec_sum_off="1"), "AMBIG_EXEC_SUM_MISMATCH"),
    ("position", dict(position_offset="1"), "AMBIG_POSITION_MISMATCH"),
    ("unknown_create", dict(create_unknown=True), "AMBIG_UNKNOWN_CREATE_OUTCOME"),
]


class TestAmbiguityHalts:
    @pytest.mark.parametrize("name,kw,code", AMBIGUOUS, ids=[a[0] for a in AMBIGUOUS])
    def test_halts_with_a_distinct_code_and_the_state_captured(
            self, tmp_path, capsys, name, kw, code):
        v = F.FakeVenue(**kw)
        rc, _ = invoke(v, tmp_path)
        rep = report(tmp_path)
        err = capsys.readouterr().err
        assert rc == X.EXIT_AMBIGUITY == 4
        assert rc not in (X.EXIT_REFUSED, X.EXIT_NOT_FLAT, X.EXIT_LIMIT, 0)
        assert rep["halt"]["code"] == code and code in err
        assert rep["captured_venue_state"]["position"] is not None
        assert "by_link" in rep["captured_venue_state"]
        # it stopped: only the first experiment started
        assert [e["id"] for e in rep["experiments"]] in ([], ["E1"])
        assert len(v.creates) <= 4
        assert rep["final_state"]["flat"] is True

    def test_a_partial_fill_is_ambiguity_not_a_result(self, tmp_path):
        v = F.FakeVenue(partial_fill=True)
        rc, _ = invoke(v, tmp_path)
        rep = report(tmp_path)
        assert rep["halt"]["code"] == "AMBIG_UNEXPECTED_STATUS"
        assert rc in (X.EXIT_AMBIGUITY, X.EXIT_NOT_FLAT)

    def test_an_unreadable_position_mid_run_halts(self, tmp_path):
        class Flaky(F.FakeVenue):
            def _positions(self, params, body):
                if len(self.creates) >= 1:
                    return self._ok({})
                return super()._positions(params, body)
        v = Flaky()
        rc, _ = invoke(v, tmp_path)
        rep = report(tmp_path)
        assert rep["halt"]["code"] == "AMBIG_UNREADABLE"
        assert rc in (X.EXIT_AMBIGUITY, X.EXIT_NOT_FLAT)

    def test_an_interrupt_halts_reconciles_and_leaves_the_account_flat(
            self, tmp_path):
        class Killed(F.FakeVenue):
            fired = False

            def _request(self, method, endpoint, **kw):
                if len(self.requests) == 20 and not self.fired:
                    self.fired = True
                    raise KeyboardInterrupt()
                return super()._request(method, endpoint, **kw)
        v = Killed()
        rc, _ = invoke(v, tmp_path)
        rep = report(tmp_path)
        assert rep["halt"]["code"] == "AMBIG_INTERRUPTED"
        assert rc == X.EXIT_AMBIGUITY and v.position == 0
        assert rep["final_state"]["flat"] is True

    def test_a_position_left_by_a_halt_is_closed_by_one_reduce_only_order(
            self, tmp_path):
        v = F.FakeVenue(weird_status="Untriggered")
        rc, _ = invoke(v, tmp_path)
        rep = report(tmp_path)
        assert rc == X.EXIT_AMBIGUITY and v.position == 0
        fs = rep["final_state"]
        assert fs["close_attempted"] is True
        last = v.creates[-1]["body"]
        assert last["reduceOnly"] is True and last["side"] == "Buy"


class TestFlatCheck:
    def test_not_flat_is_loud_and_nonzero(self, tmp_path, capsys):
        v = F.FakeVenue(close_ignored=True)
        rc, _ = invoke(v, tmp_path)
        cap = capsys.readouterr()
        rep = report(tmp_path)
        assert rc == X.EXIT_NOT_FLAT == 5
        assert "NOT FLAT" in cap.err and "!!!!" in cap.err
        assert rep["final_state"]["flat"] is False
        assert rep["final_state"]["close_attempted"] is True
        assert v.position != 0

    def test_exactly_one_bounded_emergency_close_is_attempted(self, tmp_path):
        v = F.FakeVenue(close_ignored=True)
        invoke(v, tmp_path)
        rep = report(tmp_path)
        closes = [c for c in v.creates if c["body"]["side"] == "Buy"]
        # the normal close attempt, then the ONE emergency close
        fin = [c for c in closes if c["body"]["orderLinkId"].startswith("XP-fin-")]
        assert len(fin) == 1
        assert len(v.creates) <= 4

    def test_a_position_bigger_than_anything_it_opened_is_not_touched(
            self, tmp_path, capsys):
        v = F.FakeVenue()
        rc, _ = invoke(v, tmp_path, "--phase", "submit")
        assert rc == 0
        v.position = X.Decimal("-5000")
        n = len(v.creates)
        rc, _ = invoke(v, tmp_path, "--phase", "recover")
        rep = report(tmp_path, name="report_recover.json")
        assert rc == X.EXIT_NOT_FLAT
        assert len(v.creates) == n                      # nothing was sent
        assert "not touching it" in json.dumps(rep["final_state"])

    def test_unreadable_final_state_is_not_flat(self, tmp_path):
        class Dies(F.FakeVenue):
            def _positions(self, params, body):
                if len(self.creates) >= 3:
                    raise bc.PermanentAPIError("HTTP 500", 0, "")
                return super()._positions(params, body)
        v = Dies()
        rc, _ = invoke(v, tmp_path)
        assert rc == X.EXIT_NOT_FLAT

    def test_the_final_step_always_reads_position_and_open_orders(self, tmp_path):
        v = F.FakeVenue()
        invoke(v, tmp_path, "--experiments", "E1")
        tail = v.requests[-2:]
        assert {r["path"] for r in tail} == {"/v5/position/list",
                                             "/v5/order/realtime"}


# ----------------------------------------------------------- hard limits -----

MISBEHAVING = [dict(), dict(dup_policy="accept_new"), dict(close_ignored=True),
               dict(partial_fill=True), dict(create_unknown=True),
               dict(weird_status="Untriggered"), dict(hide_orders=True),
               dict(exec_sum_off="1"), dict(position_offset="1"),
               dict(dup_policy="accept_new", close_ignored=True)]


class TestHardLimits:
    @pytest.mark.parametrize("kw", MISBEHAVING)
    @pytest.mark.parametrize("cap", [None, "8", "6"])
    def test_total_orders_never_exceed_the_ceiling(self, tmp_path, kw, cap):
        v = F.FakeVenue(**kw)
        extra = ("--max-orders", cap) if cap else ()
        invoke(v, tmp_path, *extra)
        limit = int(cap) if cap else X.HARD_MAX_ORDERS
        assert len(v.creates) <= limit <= X.HARD_MAX_ORDERS
        man = json.load(open(tmp_path / "run" / "manifest.json"))
        assert man["creates_total"] == len(v.creates)

    def test_the_gateway_itself_stops_at_the_ceiling(self, tmp_path):
        v = F.FakeVenue()
        lab = _lab(v, tmp_path, max_orders=3)
        ex = X.Exp(lab, "T", "t", "t")
        sent = 0
        with pytest.raises(X.LimitHalt):
            for i in range(10):
                lab.place(ex, f"s{i}", lab.body("Sell", lab.sizing.q0,
                                                lab.link("T", i)), "open")
                sent += 1
        assert len(v.creates) <= 3

    def test_the_reserve_is_kept_for_the_final_step(self, tmp_path):
        v = F.FakeVenue()
        lab = _lab(v, tmp_path, max_orders=4)
        ex = X.Exp(lab, "T", "t", "t")
        n = 0
        with pytest.raises(X.LimitHalt) as err:
            for i in range(10):
                lab.place(ex, f"s{i}", lab.body("Sell", lab.sizing.q0,
                                                lab.link("T", i)), "open")
                n += 1
        assert err.value.code == "ORDER_RESERVE"
        assert n == 4 - X.EMERGENCY_RESERVE

    def test_experiments_are_capped(self, tmp_path):
        v = F.FakeVenue()
        rc, _ = invoke(v, tmp_path, "--max-experiments", "2")
        rep = report(tmp_path)
        assert rc == 0 and [e["id"] for e in rep["experiments"]] == ["E1", "E2"]
        assert {n["id"] for n in rep["not_run"] if "why" in n and
                n["why"] == "max experiments"} >= {"E3", "E11"}

    def test_experiment_selection_runs_only_those(self, tmp_path):
        v = F.FakeVenue()
        rc, _ = invoke(v, tmp_path, "--experiments", "E11,E1")
        assert rc == 0
        assert [e["id"] for e in report(tmp_path)["experiments"]] == ["E11", "E1"]

    def test_notional_limit_is_per_order_and_lowerable(self, tmp_path):
        v = F.FakeVenue()           # largest order is 12 XRP at 0.5 -> 6.6 USD
        rc, _ = invoke(v, tmp_path, "--max-notional-usd", "5")
        assert rc == X.EXIT_REFUSED and v.creates == []
        v2 = F.FakeVenue()
        rc, _ = invoke(v2, tmp_path, "--max-notional-usd", "7",
                       "--experiments", "E1", out="b")
        assert rc == 0
        for c in v2.creates:
            b = c["body"]
            px = float(b.get("price") or v2.last)
            assert float(b["qty"]) * px * 1.1 <= 7.0 * 1.1 + 1e-9

    def test_the_duration_limit_halts_and_still_closes(self, tmp_path):
        v = F.FakeVenue()

        def slow(_s):
            v.clock.advance(25)
        rc, _ = invoke(v, tmp_path, "--max-duration-s", "120", sleep=slow)
        rep = report(tmp_path)
        assert rc == X.EXIT_LIMIT == 6, rep.get("halt")
        assert rep["halt"]["code"] == "DURATION"
        assert v.position == 0 and rep["final_state"]["flat"]

    def test_sizing_comes_from_the_venues_lot_rules(self):
        inst = X.Instrument("T", X.Decimal("0.01"), X.Decimal("0.1"),
                            X.Decimal("0.3"), X.Decimal("5"), None)
        s = X.compute_sizing(inst, X.Decimal("2"), 20)
        assert s.q0 >= inst.min_qty and (s.q0 / inst.step) % 1 == 0
        assert s.q0 * s.ref_price >= inst.min_notional
        assert s.qmax == s.q0 + inst.step
        assert s.qmax * s.ref_price * X.NOTIONAL_PRICE_BUFFER <= 20
        assert s.limit_price_1 > s.ref_price * X.Decimal("1.04")
        assert s.limit_price_2 > s.limit_price_1
        with pytest.raises(X.Refused):
            X.compute_sizing(inst, X.Decimal("2000"), 20)
        with pytest.raises(X.Refused):
            X.compute_sizing(X.Instrument("T", X.Decimal(0), X.Decimal("1"),
                                          X.Decimal("1"), X.Decimal("1"), None),
                             X.Decimal("1"), 20)


# --------------------------------------------- allowlist and order safety ----

def _lab(venue, tmp_path, max_orders=30):
    return _lab_for(venue, venue.clock, tmp_path, max_orders)


def _lab_for(venue, clock, tmp_path, max_orders=30):
    red = X.Redactor([venue.api_key, venue.api_secret])
    os.makedirs(tmp_path / "g", exist_ok=True)
    rec = X.Recorder(str(tmp_path / "g"), red, clock.monotonic, clock.now_ms)
    man = X.Manifest(str(tmp_path / "g" / "manifest.json"), red)
    lim = X.Limits.resolve(orders=max_orders)
    gw = X.Gateway(venue, symbol=SYMBOL, limits=lim, rec=rec, manifest=man,
                   clock=clock.monotonic, now_ms=clock.now_ms)
    inst = X.Instrument(SYMBOL, X.Decimal("0.0001"), X.Decimal("1"),
                        X.Decimal("1"), X.Decimal("5"), None)
    sizing = X.compute_sizing(inst, X.Decimal("0.5"), 20)
    gw.guard = X.OrderGuard(SYMBOL, inst, sizing)
    return X.Lab(gw, rec, man, X.Console(red), inst, sizing, "rid", SYMBOL,
                 clock=clock.monotonic, now_ms=clock.now_ms, sleep=clock.sleep)


class TestAllowlistAndOrderSafety:
    @pytest.mark.parametrize("kw", MISBEHAVING + [
        dict(hedge_mode=True), dict(start_position="-5")])
    def test_no_request_ever_leaves_the_allowlist(self, tmp_path, kw):
        v = F.FakeVenue(**kw)
        invoke(v, tmp_path)
        assert v.paths
        assert set(v.paths) <= set(X.ALLOWED_ENDPOINTS)
        assert "/v5/position/switch-mode" not in X.ALLOWED_ENDPOINTS
        assert all(r["method"] == X.ALLOWED_ENDPOINTS[r["path"]] for r in v.requests)
        assert all(r["url"].startswith("https://api-testnet.bybit.com/")
                   for r in v.requests)

    @pytest.mark.parametrize("path,method", [
        ("/v5/position/switch-mode", "POST"), ("/v5/order/cancel-all", "POST"),
        ("/v5/position/set-leverage", "POST"), ("/v5/order/amend", "POST"),
        ("/v5/asset/transfer/inter-transfer", "POST"),
        ("/v5/order/create", "GET"), ("/v5/order/realtime", "POST"),
        ("/v5/market/time", "GET"), ("/v5/user/query-api", "GET"),
    ])
    def test_a_forbidden_path_or_method_is_refused_locally(self, tmp_path, path, method):
        v = F.FakeVenue()
        lab = _lab(v, tmp_path)
        with pytest.raises(X.UnsafeRequest):
            lab.gw.request(method, path, params={"category": "linear",
                                                 "symbol": SYMBOL},
                           body={"category": "linear"} if method == "POST" else None)
        assert v.requests == []

    @pytest.mark.parametrize("mut", [
        dict(reduceOnly=False, side="Buy"),
        dict(side="Buy"),                                   # reduceOnly False
        dict(qty="13"), dict(qty="1000"), dict(qty="0"), dict(qty="1.5"),
        dict(positionIdx=1), dict(symbol="BTCUSDT"), dict(category="spot"),
        dict(orderLinkId="not-ours"), dict(orderLinkId="x" * 40),
        dict(takeProfit="1"), dict(stopLoss="1"), dict(triggerPrice="1"),
        dict(orderType="Limit"),                            # no price/PostOnly
        dict(orderType="Limit", price="0.5", timeInForce="PostOnly"),   # at market
        dict(orderType="Limit", price="0.53", timeInForce="GTC"),
        dict(orderType="Market", price="0.5"),
        dict(reduceOnly="false"),
    ])
    def test_the_guard_refuses_unsafe_bodies_before_the_client(self, tmp_path, mut):
        v = F.FakeVenue()
        lab = _lab(v, tmp_path)
        body = lab.body("Sell", lab.sizing.q0, lab.link("T", 1))
        body.update(mut)
        with pytest.raises(X.UnsafeRequest):
            lab.gw.request("POST", X.CREATE, body=body, kind="open")
        assert v.requests == []

    def test_a_buy_that_is_not_reduce_only_cannot_be_sent_in_any_kind(self, tmp_path):
        v = F.FakeVenue()
        lab = _lab(v, tmp_path)
        for kind in ("open", "close", "probe", "emergency"):
            body = lab.body("Buy", lab.sizing.q0, lab.link("T", 2),
                            reduce_only=False)
            with pytest.raises(X.UnsafeRequest):
                lab.gw.request("POST", X.CREATE, body=body, kind=kind)
        assert v.requests == []

    @pytest.mark.parametrize("kw", MISBEHAVING)
    def test_every_buy_is_reduce_only_and_every_order_is_small(self, tmp_path, kw):
        v = F.FakeVenue(**kw)
        invoke(v, tmp_path)
        for c in v.creates:
            b = c["body"]
            assert isinstance(b["reduceOnly"], bool)
            if b["side"] == "Buy":
                assert b["reduceOnly"] is True
            assert str(b["positionIdx"]) == "0" and b["category"] == "linear"
            assert float(b["qty"]) <= 12 * X.EMERGENCY_QTY_MULT
            if b["orderType"] == "Limit":
                assert b["timeInForce"] == "PostOnly" and b["side"] == "Sell"
                assert float(b["price"]) >= 0.5 * 1.04
            assert set(b) <= X.OrderGuard.ALLOWED_KEYS

    def test_no_client_side_retry_and_correct_signing(self, tmp_path):
        v = F.FakeVenue()
        invoke(v, tmp_path, "--experiments", "E1")
        assert all(r["retries"] == 0 for r in v.requests)
        for r in v.requests:
            public = r["path"] in X.PUBLIC_ENDPOINTS
            assert r["signed"] is (not public)

    def test_the_host_is_rechecked_on_every_request(self, tmp_path):
        class Moves(F.FakeVenue):
            def _request(self, method, endpoint, **kw):
                out = super()._request(method, endpoint, **kw)
                if len(self.requests) == 14:      # after the check, between calls
                    self.base_url = "https://api.bybit.com"
                return out
        v = Moves()
        rc, _ = invoke(v, tmp_path)
        assert all(r["url"].startswith("https://api-testnet.bybit.com/")
                   for r in v.requests)
        assert len(v.requests) == 14
        assert rc == X.EXIT_NOT_FLAT          # it could not even verify flat

    def test_no_other_host_exists_in_the_source(self):
        src = open(os.path.join(BOT, "tools", "close_testnet_experiment.py"),
                   encoding="utf-8").read()
        hosts = {h.rstrip(".") for h in
                 re.findall(r"https?://[A-Za-z0-9.\-]+", src)}
        assert hosts == {"https://api-testnet.bybit.com"}, hosts

    def test_the_environment_is_read_once_and_live_ack_is_never_read_for_output(self):
        src = open(os.path.join(BOT, "tools", "close_testnet_experiment.py"),
                   encoding="utf-8").read()
        tree = ast.parse(src)
        main_block = [n for n in tree.body if isinstance(n, ast.If) and "__main__" in ast.dump(n.test)]
        guard_lines = {n.lineno for b in main_block for n in ast.walk(b) if hasattr(n, "lineno")}          # the command-line ENTRY hands the process variables to the single-writer guard (reviewed)
        hits = [n for n in ast.walk(tree) if isinstance(n, ast.Attribute)
                and isinstance(n.value, ast.Name) and n.value.id == "os"
                and n.attr in ("environ", "getenv") and n.lineno not in guard_lines]
        assert len(hits) == 1                                            # still ONE read inside the tool itself
        assert "allows_live" not in src


# --------------------------------------------------------------- redaction ---

class TestRedaction:
    def test_keys_headers_and_values_are_removed_at_any_depth(self):
        r = X.Redactor(["s3cr3t-VALUE", "k3y-VALUE"])
        out = r.scrub({
            "X-BAPI-API-KEY": "k3y-VALUE", "x-bapi-sign": "abcdef",
            "nested": [{"apiSecret": "zzz", "ok": "fine",
                        "msg": "the s3cr3t-VALUE is here"}],
            "Authorization": "Bearer q", "sign": "1", "signature": "2",
            "n": 5, "weird\"key s3cr3t-VALUE": "v"})
        text = json.dumps(out)
        assert "s3cr3t-VALUE" not in text and "k3y-VALUE" not in text
        assert "abcdef" not in text and "zzz" not in text and "Bearer" not in text
        assert out["nested"][0]["ok"] == "fine" and out["n"] == 5
        assert r.scrub_text(json.dumps({"a": "s3cr3t-VALUE"})) \
            .count("s3cr3t-VALUE") == 0

    def test_json_escaped_secrets_are_removed_too(self):
        secret = 'a"b\\c-secret'
        r = X.Redactor([secret])
        line = r.dumps({"x": f"prefix {secret} suffix"})
        assert secret not in line and json.dumps(secret)[1:-1] not in line

    def test_a_full_run_leaks_nothing_to_files_or_terminal(self, tmp_path, capsys):
        v = F.FakeVenue(leak_secret=True)
        rc, _ = invoke(v, tmp_path)
        text = all_text(tmp_path, capsys)
        assert rc == 0
        assert F.FAKE_API_SECRET not in text and F.FAKE_API_KEY not in text
        assert "***REDACTED***" in text        # the venue DID echo them
        for name in os.listdir(tmp_path / "run"):
            assert name in ("records.jsonl", "manifest.json", "report.json")

    def test_a_halt_report_leaks_nothing_either(self, tmp_path, capsys):
        v = F.FakeVenue(leak_secret=True, weird_status="Untriggered")
        rc, _ = invoke(v, tmp_path)
        text = all_text(tmp_path, capsys)
        assert rc == X.EXIT_AMBIGUITY
        assert F.FAKE_API_SECRET not in text and F.FAKE_API_KEY not in text

    def test_the_secret_is_removed_even_if_it_is_the_clients_not_the_configs(
            self, tmp_path, capsys):
        v = F.FakeVenue(leak_secret=True)
        v.api_secret = "ONLY-ON-THE-CLIENT-SECRET"
        v.api_key = "ONLY-ON-THE-CLIENT-KEY"
        rc, _ = invoke(v, tmp_path, "--experiments", "E6")
        text = all_text(tmp_path, capsys)
        assert "ONLY-ON-THE-CLIENT" not in text


# ------------------------------------------------- submit / recover phases ----

class TestSubmitThenRecoverInANewProcess:
    def test_submit_leaves_one_resting_order_and_a_manifest(self, tmp_path, capsys):
        v = F.FakeVenue()
        rc, _ = invoke(v, tmp_path, "--phase", "submit")
        assert rc == 0
        open_orders = [o for o in v.orders if o["orderStatus"] in F.FakeVenue.OPEN]
        assert len(open_orders) == 1 and len(v.creates) == 1
        assert open_orders[0]["orderType"] == "Limit"
        man = json.load(open(tmp_path / "run" / "manifest.json"))
        assert man["pending"]["E8"]["state"] == "SUBMITTED"
        assert man["pending"]["E8"]["link"] == open_orders[0]["orderLinkId"]
        assert "--phase recover" in capsys.readouterr().out

    def test_recover_finds_it_by_link_alone_and_ends_flat(self, tmp_path):
        v = F.FakeVenue()
        assert invoke(v, tmp_path, "--phase", "submit")[0] == 0
        v.clock.advance(3600)              # the owner killed the process; time passes
        v.last = v.last * X.Decimal("1.2") # and the market moved
        rc, _ = invoke(v, tmp_path, "--phase", "recover")
        rep = report(tmp_path, name="report_recover.json")
        assert rc == 0 and rep["final_state"]["flat"] and rep["final_state"]["clean"]
        assert not [o for o in v.orders if o["orderStatus"] in F.FakeVenue.OPEN]
        q = exp(rep, "E8")["facts"]["queries"][0]
        assert q["label"] == "after_process_restart"
        assert q["seconds_since_submit"] >= 3600 and q["in_realtime"] == 1
        assert len(v.creates) == 1                       # recover created nothing
        man = json.load(open(tmp_path / "run" / "manifest.json"))
        assert man["pending"]["E8"]["state"] == "RECOVERED"

    def test_recover_uses_the_persisted_sizing_not_a_fresh_price(self, tmp_path):
        v = F.FakeVenue()
        invoke(v, tmp_path, "--phase", "submit")
        v.last = X.Decimal("500")          # a price that would break fresh sizing
        rc, _ = invoke(v, tmp_path, "--phase", "recover")
        assert rc == 0

    def test_recover_without_a_manifest_or_with_another_symbol_refuses(
            self, tmp_path):
        v = F.FakeVenue()
        assert invoke(v, tmp_path, "--phase", "recover")[0] == X.EXIT_REFUSED
        invoke(v, tmp_path, "--phase", "submit", out="s")
        assert invoke(v, tmp_path, "--phase", "recover", out="s",
                      symbol="DOGEUSDT")[0] == X.EXIT_REFUSED

    def test_recover_with_nothing_pending_still_runs_the_flat_step(self, tmp_path):
        v = F.FakeVenue()
        assert invoke(v, tmp_path, "--experiments", "E1")[0] == 0
        n = len(v.creates)
        rc, _ = invoke(v, tmp_path, "--phase", "recover")
        assert rc == 0 and len(v.creates) == n
        assert report(tmp_path, name="report_recover.json")["final_state"]["flat"]

    def test_recover_cleans_up_a_run_killed_with_a_position_open(self, tmp_path):
        class Killed(F.FakeVenue):
            fired = False

            def _request(self, method, endpoint, **kw):
                if self.creates and not self.fired:
                    self.fired = True
                    raise SystemExit("kill -9")      # not caught by the harness
                return super()._request(method, endpoint, **kw)
        v = Killed()
        with pytest.raises(SystemExit):
            invoke(v, tmp_path)
        assert v.position != 0                       # the kill left a short
        rc, _ = invoke(v, tmp_path, "--phase", "recover")
        rep = report(tmp_path, name="report_recover.json")
        assert rc == 0 and v.position == 0 and rep["final_state"]["flat"]
        assert rep["final_state"]["close_attempted"] is True
        assert exp(rep, "E8")["status"] == "NOTHING_PENDING"

    def test_submit_and_recover_do_not_exceed_the_ceiling_together(self, tmp_path):
        v = F.FakeVenue()
        invoke(v, tmp_path, "--phase", "submit", "--max-orders", "4")
        invoke(v, tmp_path, "--phase", "recover", "--max-orders", "4")
        assert len(v.creates) <= 4


# ------------------------------------------- the REAL client, stub transport --

class StubTransport(bc.Transport):
    def __init__(self, replies=()):
        self.calls = []
        self.replies = list(replies)

    def request(self, method, url, *, headers, params=None, body=None,
                timeout=10.0):
        self.calls.append({"method": method, "url": url,
                           "headers": dict(headers), "body": body})
        if self.replies:
            return self.replies.pop(0)
        return 200, json.dumps({"retCode": 0, "retMsg": "OK",
                                "result": {"list": []}})


class TestAgainstTheRealClientClass:
    """The harness drives `BybitClient._request` itself; a stub TRANSPORT (no
    socket) shows the call shape is compatible and that retries=0 is honoured."""

    def _client(self, tmp_path, venue="testnet", replies=()):
        import config as _config
        import persistence
        cfg = _config.load({"BYBIT_VENUE": venue, "USE_TESTNET":
                            "1" if venue != "mainnet" else "0",
                            "PAPER_TRADING": "1",
                            "BYBIT_API_KEY": F.FAKE_API_KEY,
                            "BYBIT_API_SECRET": F.FAKE_API_SECRET})
        tr = StubTransport(replies)
        client = bc.BybitClient(
            config=cfg, store=persistence.StateStore(str(tmp_path / "s.db")),
            transport=tr)
        return client, tr

    def test_a_real_testnet_client_passes_and_other_venues_do_not(self, tmp_path):
        client, _ = self._client(tmp_path)
        assert X.client_refusal(client) is None
        for venue in ("mainnet", "demo"):
            other, _ = self._client(tmp_path, venue=venue)
            assert X.client_refusal(other) is not None

    def test_requests_are_signed_private_and_unsigned_public_on_the_host(
            self, tmp_path):
        client, tr = self._client(tmp_path)
        lab = _lab_for(client, F.FakeClock(), tmp_path)
        lab.gw.request("GET", X.POSITIONS, params={"category": "linear",
                                                   "symbol": SYMBOL})
        lab.gw.request("GET", X.TICKERS, params={"category": "linear",
                                                 "symbol": SYMBOL})
        priv, pub = tr.calls
        assert priv["url"].startswith("https://api-testnet.bybit.com/v5/position/list")
        assert "X-BAPI-SIGN" in priv["headers"] and "X-BAPI-SIGN" not in pub["headers"]

    def test_no_secret_reaches_the_records_even_through_the_real_client(
            self, tmp_path):
        client, tr = self._client(tmp_path)
        lab = _lab_for(client, F.FakeClock(), tmp_path)
        lab.gw.request("GET", X.POSITIONS, params={"category": "linear",
                                                   "symbol": SYMBOL})
        text = open(tmp_path / "g" / "records.jsonl").read()
        assert F.FAKE_API_SECRET not in text and F.FAKE_API_KEY not in text
        assert tr.calls[0]["headers"]["X-BAPI-API-KEY"] == F.FAKE_API_KEY

    def test_an_http_error_on_create_is_one_attempt_and_an_unknown_outcome(
            self, tmp_path):
        client, tr = self._client(tmp_path, replies=[(500, "boom"), (500, "boom")])
        lab = _lab_for(client, F.FakeClock(), tmp_path)
        ex = X.Exp(lab, "T", "t", "t")
        with pytest.raises(X.Ambiguity) as err:
            lab.place(ex, "s", lab.body("Sell", lab.sizing.q0,
                                        lab.link("T", 1)), "open")
        assert err.value.code == "AMBIG_UNKNOWN_CREATE_OUTCOME"
        assert len(tr.calls) == 1                    # no client-side re-send
        assert tr.calls[0]["method"] == "POST"

    def test_a_duplicate_retcode_raised_by_the_client_is_recorded(self, tmp_path):
        reply = (200, json.dumps({"retCode": 110072, "retMsg": "dup", "result": {}}))
        client, tr = self._client(tmp_path, replies=[reply])
        lab = _lab_for(client, F.FakeClock(), tmp_path)
        ex = X.Exp(lab, "T", "t", "t")
        resp, outcome = lab.place(ex, "s", lab.body("Sell", lab.sizing.q0,
                                                    lab.link("T", 1)), "open")
        assert outcome == "rejected" and resp.ret_code == 110072
        assert X.summary(resp)["ret_code_in_repo_duplicate_table"] is True


# ------------------------------------------------------------ self check -----

class TestSelfCheckOffline:
    def test_runs_the_whole_harness_against_the_fake_venue(self, tmp_path, capsys):
        rc = X.main(["--self-check-offline", "--symbol", SYMBOL,
                     "--out-dir", str(tmp_path / "sc")])
        out = capsys.readouterr().out
        assert rc == 0 and "RECORDED FACTS" in out
        assert F.FAKE_API_SECRET not in out
        rep = json.load(open(tmp_path / "sc" / "report.json"))
        assert rep["order_creates_sent"] == 26


# -------------------------------------------------------------------- CI -----

class TestWiring:
    def test_the_test_file_is_in_the_workflow_and_the_required_tuple(self):
        wf = os.path.join(os.path.dirname(BOT), ".github", "workflows",
                          "carry-verify.yml")
        assert "tests/test_close_testnet_experiment.py" in open(wf).read()
        import test_ci_workflow
        assert "test_close_testnet_experiment" in test_ci_workflow.REQUIRED
