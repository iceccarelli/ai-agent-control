"""The machine-readable performance report: every number says where it came from.

`tools/carry_performance.py --json` is the one report. Each section carries an
evidence class from a closed set; only a mainnet ledger the database
corroborates, with every fee venue-stated and every funding lifecycle
reconciled, can produce ACTUAL_RECONCILED, and only there may a realized-cash
key appear. Figures are a hand-computed FIXTURE, not an observation.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))

import ledger as L                      # noqa: E402
import carry_performance as perf        # noqa: E402
import performance_analytics as PA      # noqa: E402
from persistence import StateStore, PersistenceError  # noqa: E402

DAY = 86_400_000
T0 = 1_700_000_000_000
NOW = T0 + 60 * DAY


def overlay_cycle(j, d0, d1, *, tag, funding=300.0, close_price=98_000.0,
                  fee_status="confirmed", close_fee_status=None,
                  basis="venue", reconcile=True, qty=1.0):
    """A hedged overlay cycle: short at 100k, venue funding, cover. Net with
    the defaults: 300 - 110 + 2000 = 2190."""
    L.perp_open(j, ms=T0 + d0 * DAY, ref=f"{tag}-o", qty=qty, price=100_000.0,
                fee_usd=55.0, fee_status=fee_status)
    L.funding(j, ms=T0 + (d0 + 1) * DAY, ref=f"{tag}-f", usd=funding,
              basis=basis)
    L.perp_close(j, ms=T0 + d1 * DAY, ref=f"{tag}-c", qty=qty,
                 price=close_price, entry_price=100_000.0, fee_usd=55.0,
                 fee_status=close_fee_status or fee_status)
    if reconcile:
        reconcile_last(j, ms=T0 + d1 * DAY + 1)


def reconcile_last(j, *, ms):
    """A FULLY_RECONCILED record for the newest lifecycle, as
    funding_reconcile writes one (fingerprint of the exposure it judged)."""
    lc = L.lifecycles(j)[-1]
    j.post(L.Entry(
        ms=ms, kind=L.RECONCILIATION_KIND, ref=f"fundrec:{lc.id}:{ms}",
        lines=[L.Line(L.USDT, 0.0), L.Line(L.FUNDING, 0.0)],
        fee_raw={"lifecycle_id": lc.id, "state": "FULLY_RECONCILED",
                 "reasons": [], "fingerprint": list(lc.fingerprint),
                 "judged_to_ms": lc.end_ms, "as_of_ms": ms}))


def write_db(tmp_path, journal, *, envs=("mainnet",), margin=10_000.0,
             unresolved_order=False, name="s.db"):
    db = str(tmp_path / name)
    store = StateStore(db)
    store.save_ledger(journal.to_rows())
    for env in envs:
        store.record_execution_environment(env)
    for c in perf.cycles_from_journal(journal):
        cid = perf._cycle_ms(c["cycle_id"])
        if margin is not None and cid is not None:
            store.record_risk_observation({
                "ms": cid + 1, "symbol": "BTCUSDT", "cycle_opened_ms": cid,
                "position_im_usd": margin})
    if unresolved_order:
        store.record_order("carry-x-1", "BTCUSDT", "Sell", "Market", 1.0,
                           purpose="carry-perp", status="unknown")
    store.close()
    return db


def report(db, **kw):
    kw.setdefault("now_ms", NOW)
    return perf.build_report(db, **kw)


@pytest.fixture()
def clean():
    j = L.Journal()
    overlay_cycle(j, 0, 10, tag="a")
    return j


# -- the environment decides whether anything can be cash ---------------------

def test_reconciled_mainnet_is_actual_reconciled_and_matches_the_statement(tmp_path, clean):
    r = report(write_db(tmp_path, clean), environment="mainnet")
    rc = r["sections"]["realized_cash"]
    assert rc["evidence_class"] == "ACTUAL_RECONCILED"
    assert rc["realized_cash_net_usd"] == pytest.approx(2190.0)
    assert rc["realized_cash_net_usd"] == pytest.approx(
        clean.statement(now_ms=NOW)["net_usd"])
    assert r["sections"]["ledger_detail"]["evidence_class"] == "ACTUAL_RECONCILED"
    assert r["sections"]["closed_cycles_booked"]["cycle_count"] == 0


def test_undeclared_environment_is_never_cash(tmp_path, clean):
    r = report(write_db(tmp_path, clean))
    assert r["environment"]["booked_figures_class"] == "UNDECLARED_ENVIRONMENT"
    rc = r["sections"]["realized_cash"]
    assert rc["evidence_class"] == "UNAVAILABLE"
    assert "realized_cash_net_usd" not in rc
    assert any("ENVIRONMENT_NOT_DECLARED" in w for w in rc["why"])
    assert r["sections"]["closed_cycles_booked"]["evidence_class"] == \
        "UNDECLARED_ENVIRONMENT"


@pytest.mark.parametrize("env,cls", [("testnet", "PAPER_EXECUTION"),
                                     ("paper", "PAPER_EXECUTION")])
def test_a_testnet_or_paper_balance_is_never_cash(tmp_path, clean, env, cls):
    # every fee confirmed and every lifecycle reconciled: still not cash
    r = report(write_db(tmp_path, clean, envs=(env,)), environment=env)
    assert r["environment"]["actual_reconciled_eligible"] is False
    assert r["sections"]["realized_cash"]["evidence_class"] == "UNAVAILABLE"
    closed = r["sections"]["closed_cycles_booked"]
    assert closed["evidence_class"] == cls
    assert closed["booked_net_usd_total"] == pytest.approx(2190.0)
    for name, sec in r["sections"].items():
        assert sec["evidence_class"] != "ACTUAL_RECONCILED", name
    assert perf._cash_keys({k: v for k, v in r["sections"].items()
                            if k != "realized_cash"}) == []


def test_simulation_ledgers_carry_their_simulation_class(tmp_path, clean):
    db = write_db(tmp_path, clean, envs=())
    for env, cls in (("historical_simulation", "HISTORICAL_SIMULATION"),
                     ("forward_simulation", "FORWARD_SIMULATION")):
        r = report(db, environment=env)
        assert r["sections"]["closed_cycles_booked"]["evidence_class"] == cls
        assert r["sections"]["realized_cash"]["evidence_class"] == "UNAVAILABLE"


def test_mainnet_declared_without_a_database_record_is_not_cash(tmp_path, clean):
    r = report(write_db(tmp_path, clean, envs=()), environment="mainnet")
    assert r["sections"]["realized_cash"]["evidence_class"] == "UNAVAILABLE"
    assert any("ENVIRONMENT_NOT_RECORDED" in w
               for w in r["sections"]["realized_cash"]["why"])


def test_a_declaration_the_database_contradicts_is_unavailable(tmp_path, clean):
    # the same database once booked testnet orders: its ledger is mixed
    r = report(write_db(tmp_path, clean, envs=("testnet", "mainnet")),
               environment="mainnet")
    assert r["environment"]["booked_figures_class"] == "UNAVAILABLE"
    assert r["sections"]["realized_cash"]["evidence_class"] == "UNAVAILABLE"
    assert any("CONTRADICTS" in w for w in r["environment"]["reasons"])
    # a simulation declared over a database that recorded an execution venue
    r2 = report(write_db(tmp_path, clean, envs=("testnet",), name="b.db"),
                environment="historical_simulation")
    assert r2["environment"]["booked_figures_class"] == "UNAVAILABLE"


def test_equity_scope_testnet_contradicts_a_mainnet_declaration():
    env = perf.classify_environment(
        "mainnet", {"seen": ["mainnet"], "equity_scope":
                    "venue=bybit;testnet=True"})
    assert env["actual_reconciled_eligible"] is False
    assert env["booked_figures_class"] == "UNAVAILABLE"


# -- accounting invariants ----------------------------------------------------

def test_an_unknown_fee_is_never_zero(tmp_path):
    j = L.Journal()
    overlay_cycle(j, 0, 10, tag="a", fee_status="unknown")
    r = report(write_db(tmp_path, j), environment="mainnet")
    assert r["sections"]["realized_cash"]["evidence_class"] == "UNAVAILABLE"
    c = r["sections"]["closed_cycles_booked"]["cycles"][0]
    assert c["fees_usd"] is None and c["fees_complete"] is False
    assert c["fees_unconfirmed_fills"] == 2
    assert c["net_usd"] is None                     # no final net either
    assert any("FEES_INCOMPLETE" in w for w in c["provisional_reasons"])


def test_missing_funding_is_never_zero(tmp_path):
    j = L.Journal()
    # funding booked from the venue, but no reconciliation ever ran
    overlay_cycle(j, 0, 10, tag="a", reconcile=False)
    r = report(write_db(tmp_path, j), environment="mainnet")
    c = r["sections"]["closed_cycles_booked"]["cycles"][0]
    assert c["funding_usd"] is None and c["funding_complete"] is False
    assert c["funding_booked_usd"] == pytest.approx(300.0)
    assert any("FUNDING_NOT_RECONCILED" in w for w in c["provisional_reasons"])
    # a cycle with NO funding entry at all is not "funding = 0" either
    j2 = L.Journal()
    L.perp_open(j2, ms=T0, ref="o", qty=1.0, price=100_000.0, fee_usd=55.0,
                fee_status="confirmed")
    L.perp_close(j2, ms=T0 + 10 * DAY, ref="c", qty=1.0, price=100_000.0,
                 entry_price=100_000.0, fee_usd=55.0, fee_status="confirmed")
    r2 = report(write_db(tmp_path, j2, name="b.db"), environment="mainnet")
    c2 = r2["sections"]["closed_cycles_booked"]["cycles"][0]
    assert c2["funding_usd"] is None


def test_engine_estimated_funding_is_not_actual(tmp_path):
    j = L.Journal()
    overlay_cycle(j, 0, 10, tag="a", basis="engine_estimate")
    r = report(write_db(tmp_path, j), environment="mainnet")
    c = r["sections"]["closed_cycles_booked"]["cycles"][0]
    assert c["funding_usd"] is None
    assert any("FUNDING_NOT_VENUE_STATED" in w for w in c["provisional_reasons"])


def test_an_unconfirmed_fill_is_not_a_completed_position(tmp_path):
    j = L.Journal()
    overlay_cycle(j, 0, 10, tag="a")
    # opened 1.0, only 0.4 covered: the position is not complete
    L.perp_open(j, ms=T0 + 20 * DAY, ref="b-o", qty=1.0, price=100_000.0,
                fee_usd=55.0, fee_status="confirmed")
    L.perp_close(j, ms=T0 + 21 * DAY, ref="b-c", qty=0.4, price=99_000.0,
                 entry_price=100_000.0, fee_usd=22.0, fee_status="confirmed")
    r = report(write_db(tmp_path, j), environment="mainnet")
    rc = r["sections"]["realized_cash"]
    assert rc["realized_cash_cycles"] == 1
    assert rc["realized_cash_net_usd"] == pytest.approx(2190.0)  # b excluded
    opened = r["sections"]["open_positions"]
    assert opened["evidence_class"] == "UNAVAILABLE"
    assert [c["outcome"] for c in opened["cycles"]] == ["OPEN"]
    assert opened["cycles"][0]["net_usd"] is None
    assert opened["open_perp_qty"] == pytest.approx(-0.6)
    # the open cycle forbids calling the whole ledger reconciled
    assert r["sections"]["ledger_detail"]["evidence_class"] != "ACTUAL_RECONCILED"


def test_an_unresolved_close_is_not_realized_profit(tmp_path):
    j = L.Journal()
    overlay_cycle(j, 0, 10, tag="a", close_fee_status="unresolved")
    r = report(write_db(tmp_path, j), environment="mainnet")
    assert r["sections"]["realized_cash"]["evidence_class"] == "UNAVAILABLE"
    c = r["sections"]["closed_cycles_booked"]["cycles"][0]
    assert c["price_leg_usd"] == pytest.approx(2000.0)    # booked, not cash
    assert c["net_usd"] is None


def test_an_unresolved_carry_order_blocks_realized_cash(tmp_path, clean):
    r = report(write_db(tmp_path, clean, unresolved_order=True),
               environment="mainnet")
    assert r["unresolved_carry_orders"] == 1
    assert r["sections"]["realized_cash"]["evidence_class"] == "UNAVAILABLE"
    assert any("UNRESOLVED_CARRY_ORDERS" in w
               for w in r["sections"]["realized_cash"]["why"])


def test_cycle_net_equals_the_statement_net(clean):
    cycles = perf.cycles_from_journal(clean)
    inv = {i["name"]: i for i in perf.check_invariants(
        clean, cycles, clean.statement())}
    assert inv["cycle_net_equals_statement_net"]["ok"]
    cycles[0]["net_usd"] += 0.01                   # a cycle that lost a cent
    inv = {i["name"]: i for i in perf.check_invariants(
        clean, cycles, clean.statement())}
    assert not inv["cycle_net_equals_statement_net"]["ok"]


def test_a_cycle_belongs_to_exactly_one_id(tmp_path):
    j = L.Journal()
    overlay_cycle(j, 0, 10, tag="a", reconcile=False)
    overlay_cycle(j, 12, 20, tag="b", reconcile=False)
    inv = {i["name"]: i["ok"] for i in perf.check_invariants(
        j, perf.cycles_from_journal(j), j.statement())}
    assert all(inv.values())
    # a stored row that puts a later entry back into the FIRST cycle
    rows = j.to_rows()
    first = rows[0]["cycle"]
    rows.append({"ms": T0 + 30 * DAY, "kind": "borrow", "ref": "late",
                 "memo": "", "fee_status": "", "fee_raw": {}, "basis": "",
                 "cycle": first,
                 "lines": [{"account": L.BORROW, "usd": 1.0, "qty": 0.0},
                           {"account": L.USDT, "usd": -1.0, "qty": -1.0}]})
    bad = L.Journal.from_rows(rows)
    inv = {i["name"]: i["ok"] for i in perf.check_invariants(
        bad, perf.cycles_from_journal(bad), bad.statement())}
    assert inv["each_entry_in_exactly_one_contiguous_cycle"] is False
    dup = perf.cycles_from_journal(j)
    inv = {i["name"]: i["ok"] for i in perf.check_invariants(
        j, dup + [dict(dup[0], net_usd=0.0)], j.statement())}
    assert inv["cycle_ids_unique"] is False


def test_a_violated_invariant_refuses_realized_cash(tmp_path):
    j = L.Journal()
    overlay_cycle(j, 0, 10, tag="a")
    rows = j.to_rows()
    rows.append({"ms": T0 + 11 * DAY, "kind": "borrow", "ref": "x",
                 "memo": "", "fee_status": "", "fee_raw": {}, "basis": "",
                 "cycle": "cyc-other",
                 "lines": [{"account": L.BORROW, "usd": 1.0, "qty": 0.0},
                           {"account": L.USDT, "usd": -1.0, "qty": -1.0}]})
    rows.append({"ms": T0 + 12 * DAY, "kind": "borrow", "ref": "y",
                 "memo": "", "fee_status": "", "fee_raw": {}, "basis": "",
                 "cycle": rows[0]["cycle"],
                 "lines": [{"account": L.BORROW, "usd": 1.0, "qty": 0.0},
                           {"account": L.USDT, "usd": -1.0, "qty": -1.0}]})
    bad = L.Journal.from_rows(rows)
    r = report(write_db(tmp_path, bad), environment="mainnet")
    assert r["invariants_hold"] is False
    assert r["sections"]["realized_cash"]["evidence_class"] == "UNAVAILABLE"
    assert any("INVARIANT_VIOLATED" in w
               for w in r["sections"]["realized_cash"]["why"])


# -- structural: a non-cash figure cannot sit under a cash key ----------------

def test_section_refuses_a_cash_key_outside_actual_reconciled():
    for cls in ("PAPER_EXECUTION", "HISTORICAL_SIMULATION",
                "FORWARD_SIMULATION", "OPEN_MARK_TO_MARKET"):
        with pytest.raises(perf.ReportIntegrityError):
            perf.section(cls, realized_cash_net_usd=1.0)
        with pytest.raises(perf.ReportIntegrityError):
            perf.section(cls, nested={"rows": [{"cash_usd": 1.0}]})
    with pytest.raises(perf.ReportIntegrityError):
        perf.section("UNAVAILABLE", why=["x"], realised_net=1.0)
    assert perf.section("ACTUAL_RECONCILED", realized_cash_net_usd=1.0)


def test_section_refuses_an_unknown_class_and_an_unexplained_gap():
    with pytest.raises(perf.ReportIntegrityError):
        perf.section("REALISED", x=1)
    with pytest.raises(perf.ReportIntegrityError):
        perf.section("UNAVAILABLE")


def test_validate_report_catches_a_section_edited_after_construction(tmp_path, clean):
    r = report(write_db(tmp_path, clean, envs=("testnet",)), environment="testnet")
    perf.validate_report(r)
    r["sections"]["closed_cycles_booked"]["realized_cash_net_usd"] = 1.0
    with pytest.raises(perf.ReportIntegrityError):
        perf.validate_report(r)
    r2 = report(write_db(tmp_path, clean, envs=("testnet",), name="b.db"),
                environment="testnet")
    r2["sections"]["closed_cycles_booked"]["evidence_class"] = \
        "ACTUAL_RECONCILED"
    with pytest.raises(perf.ReportIntegrityError):
        perf.validate_report(r2)


# -- slippage and spread: unavailable, never zero ------------------------------

def test_slippage_and_spread_are_unavailable_without_marks(tmp_path, clean):
    r = report(write_db(tmp_path, clean), environment="mainnet")
    s = r["sections"]["slippage"]
    assert s["evidence_class"] == "UNAVAILABLE"
    assert "slippage_cost_usd" not in s
    assert r["sections"]["spread_cost"]["evidence_class"] == "UNAVAILABLE"


def test_slippage_is_measured_from_journalled_decision_marks(tmp_path):
    j = L.Journal()
    L.perp_open(j, ms=T0, ref="a-o", qty=1.0, price=100_000.0, fee_usd=55.0,
                fee_status="confirmed", decision_mark=100_010.0)  # sold 10 low
    L.funding(j, ms=T0 + DAY, ref="a-f", usd=300.0, basis="venue")
    L.perp_close(j, ms=T0 + 10 * DAY, ref="a-c", qty=1.0, price=98_000.0,
                 entry_price=100_000.0, fee_usd=55.0, fee_status="confirmed",
                 decision_mark=97_995.0)                           # bought 5 high
    reconcile_last(j, ms=T0 + 10 * DAY + 1)
    r = report(write_db(tmp_path, j), environment="mainnet")
    s = r["sections"]["slippage"]
    assert s["measured_fills"] == 2
    assert s["slippage_cost_usd"] == pytest.approx(15.0)
    assert s["evidence_class"] == "ACTUAL_RECONCILED"
    rt = report(write_db(tmp_path, j, envs=("testnet",), name="t.db"),
                environment="testnet")
    assert rt["sections"]["slippage"]["evidence_class"] == "PAPER_EXECUTION"


# -- opportunity cost ----------------------------------------------------------

def test_no_benchmark_rate_is_ever_invented(tmp_path, clean):
    r = report(write_db(tmp_path, clean), environment="mainnet")
    opp = r["sections"]["opportunity_cost"]
    assert opp["benchmark_apr_pct"] is None
    c = opp["cycles"][0]
    assert c["vs_benchmark_rate"]["available"] is False
    assert "no benchmark rate" in c["vs_benchmark_rate"]["why"]
    assert c["vs_benchmark_rate"]["evidence_class"] == "UNAVAILABLE"


def test_overlay_against_unhedged_btc_and_an_operator_rate(tmp_path, clean):
    r = report(write_db(tmp_path, clean), environment="mainnet",
               cash_apr_pct=5.0, cash_apr_source="operator: T-bill 2023-11")
    opp = r["sections"]["opportunity_cost"]
    assert opp["evidence_class"] == "HISTORICAL_SIMULATION"
    c = opp["cycles"][0]
    assert c["overlay_leg_evidence_class"] == "ACTUAL_RECONCILED"
    u = c["vs_unhedged_btc"]
    assert u["unhedged_btc_pnl_usd"] == pytest.approx(-2000.0)   # 100k -> 98k
    assert u["hedged_pair_pnl_usd"] == pytest.approx(190.0)
    assert u["incremental_vs_unhedged_usd"] == pytest.approx(2190.0)
    assert u["denominator_usd"] == pytest.approx(100_000.0)
    assert "perp" in u["price_basis"] and "time_basis" in u
    b = c["vs_benchmark_rate"]
    assert b["denominator_usd"] == pytest.approx(110_000.0)       # 10k + 100k
    assert b["benchmark_alternative_usd"] == pytest.approx(
        110_000.0 * 0.05 * 10 / 365)
    assert b["incremental_vs_benchmark_usd"] == pytest.approx(
        190.0 - 110_000.0 * 0.05 * 10 / 365)
    assert b["benchmark_source"] == "operator: T-bill 2023-11"
    assert "not annualised" in b["time_basis"]


@pytest.mark.parametrize("apr", [float("nan"), float("inf"), -100.0, 250.0])
def test_a_nonsense_benchmark_rate_is_refused(apr):
    c = {"cycle_id": "c", "outcome": "CLOSED", "evidence_class": "X",
         "overlay": True, "hedged_qty": 1.0, "perp_entry_price": 1.0,
         "perp_exit_price": 1.0, "hold_days": 1.0, "net_usd": 1.0,
         "revalue_usd": 0.0, "capital_committed_usd": 10.0,
         "fees_complete": True, "funding_complete": True}
    out = PA.overlay_opportunity_cost(c, benchmark_apr_pct=apr)
    assert out["vs_benchmark_rate"]["available"] is False


def test_benchmark_without_observed_margin_is_unavailable(tmp_path, clean):
    r = report(write_db(tmp_path, clean, margin=None), environment="mainnet",
               cash_apr_pct=5.0)
    b = r["sections"]["opportunity_cost"]["cycles"][0]["vs_benchmark_rate"]
    assert b["available"] is False and "margin" in b["why"]


def test_a_bought_spot_cycle_has_no_unhedged_counterfactual():
    c = {"cycle_id": "c", "outcome": "CLOSED", "evidence_class": "X",
         "overlay": False, "hedged_qty": 1.0, "perp_entry_price": 1.0,
         "perp_exit_price": 1.0, "hold_days": 1.0, "net_usd": 1.0,
         "revalue_usd": 0.0, "capital_committed_usd": 10.0,
         "fees_complete": True, "funding_complete": True}
    out = PA.overlay_opportunity_cost(c, benchmark_apr_pct=None)
    assert out["vs_unhedged_btc"]["available"] is False
    assert out["evidence_class"] == "UNAVAILABLE"


def test_a_provisional_overlay_leg_is_never_upgraded(tmp_path):
    j = L.Journal()
    overlay_cycle(j, 0, 10, tag="a", reconcile=False)
    r = report(write_db(tmp_path, j), environment="mainnet", cash_apr_pct=5.0)
    c = r["sections"]["opportunity_cost"]["cycles"][0]
    assert c["overlay_leg_evidence_class"] == "UNAVAILABLE"
    assert c["overlay_increment_incomplete"] == ["funding"]


# -- the decision-time mark is additive ----------------------------------------

def test_old_ledger_rows_load_and_serialise_identically():
    j = L.Journal()
    overlay_cycle(j, 0, 10, tag="a")
    rows = j.to_rows()
    assert all("decision_mark" not in r for r in rows)
    again = L.Journal.from_rows(json.loads(json.dumps(rows))).to_rows()
    assert again == rows
    assert all(e.decision_mark is None
               for e in L.Journal.from_rows(rows).entries)


def test_a_decision_mark_round_trips_and_a_bad_one_is_refused():
    j = L.Journal()
    L.perp_open(j, ms=T0, ref="o", qty=1.0, price=100.0, fee_usd=0.1,
                fee_status="confirmed", decision_mark=101.0)
    rows = j.to_rows()
    assert rows[0]["decision_mark"] == 101.0
    assert L.Journal.from_rows(rows).entries[0].decision_mark == 101.0
    for bad in (float("nan"), 0.0, -1.0):
        with pytest.raises(L.LedgerError):
            L.perp_open(L.Journal(), ms=T0, ref="o", qty=1.0, price=100.0,
                        fee_usd=0.1, decision_mark=bad)


def test_the_broker_journals_a_decision_mark_only_when_supplied():
    class Inner:
        def __init__(self, mark):
            self.mark = mark

        def place_market(self, **kw):
            out = {"filled_qty": 1.0, "avg_price": 100.0,
                   "order_link_id": f"x{kw['side']}",
                   "fee_detail": {"components": [
                       {"amount": 0.1, "currency": "USDT"}], "known": True,
                       "source": "execFee"}}
            if self.mark is not None:
                out["decision_mark"] = self.mark
            return out

    for mark, want in ((99.5, 99.5), (None, None), ("junk", None),
                       (float("nan"), None)):
        j = L.Journal()
        b = L.LedgerBroker(Inner(mark), j, ms=lambda: T0)
        b.place_market(symbol="BTCUSDT", side="Sell", qty=1.0, product="linear")
        assert j.entries[-1].decision_mark == want


# -- persistence: the execution environment record -----------------------------

def test_the_execution_environment_record_is_additive_and_never_shrinks(tmp_path):
    store = StateStore(str(tmp_path / "e.db"))
    assert store.execution_environment() is None
    assert store.execution_environments_seen() == []
    assert store.record_execution_environment("testnet") == ["testnet"]
    assert store.record_execution_environment("mainnet") == ["testnet", "mainnet"]
    assert store.record_execution_environment("mainnet") == ["testnet", "mainnet"]
    assert store.execution_environment() == "mainnet"
    with pytest.raises(PersistenceError):
        store.record_execution_environment("live")
    store.close()


# -- the CLI ------------------------------------------------------------------

def test_cli_json_is_the_report_and_reads_only(tmp_path, clean, capsys):
    db = write_db(tmp_path, clean, envs=("testnet",))
    before = os.path.getmtime(db)
    assert perf.main(["--db", db, "--json", "--environment", "testnet",
                      "--now-ms", str(NOW)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["report_schema"] == perf.REPORT_SCHEMA
    for sec in out["sections"].values():
        assert sec["evidence_class"] in perf.SECTION_CLASSES
    assert out["sections"]["realized_cash"]["evidence_class"] == "UNAVAILABLE"
    assert os.path.getmtime(db) == before


def test_cli_text_says_nothing_is_cash_when_undeclared(tmp_path, clean, capsys):
    db = write_db(tmp_path, clean)
    assert perf.main(["--db", db, "--now-ms", str(NOW)]) == 0
    out = capsys.readouterr().out
    assert "NOTHING BELOW IS REALIZED CASH" in out
    assert "UNDECLARED_ENVIRONMENT" in out


def test_cli_rejects_an_unknown_environment(tmp_path, clean):
    db = write_db(tmp_path, clean)
    with pytest.raises(SystemExit):
        perf.main(["--db", db, "--environment", "live"])


# -- carry_report carries the same evidence class ------------------------------

def test_carry_report_realised_status_is_not_cash_on_testnet(tmp_path, clean):
    import carry_report
    db = write_db(tmp_path, clean, envs=("testnet",))
    rep = carry_report.build_report(db, environment="testnet")
    assert rep["carry"]["status"] == "REALISED"        # accounting complete
    assert rep["carry"]["evidence_class"] == "PAPER_EXECUTION"
    assert rep["ordinary_trades"]["evidence_class"] == "PAPER_EXECUTION"
    undeclared = carry_report.build_report(db)
    assert undeclared["carry"]["evidence_class"] == "UNDECLARED_ENVIRONMENT"
    main = carry_report.build_report(
        write_db(tmp_path, clean, name="m.db"), environment="mainnet")
    assert main["carry"]["evidence_class"] == "ACTUAL_RECONCILED"


def test_carry_report_withholds_actual_with_an_unresolved_order(tmp_path, clean):
    import carry_report
    db = write_db(tmp_path, clean, unresolved_order=True)
    rep = carry_report.build_report(db, environment="mainnet")
    assert rep["carry"]["unresolved_carry_orders"] == 1
    assert rep["carry"]["evidence_class"] == "UNAVAILABLE"


def _journal_with_perp_fill(link="carry-x-perp-open", qty=0.01):
    import ledger as L
    j = L.Journal()
    L.perp_open(j, ms=1, ref=link, qty=qty, price=60000.0, fee_usd=0.1,
                fee_status="confirmed")
    return j


def test_order_fill_equal_to_ledger_fill_holds():
    j = _journal_with_perp_fill()
    inv = perf.order_fills_match_ledger(
        j, [{"link": "carry-x-perp-open", "status": "filled",
             "filled_qty": 0.01}])
    assert inv["ok"], inv


def test_resolved_quantity_that_differs_from_the_ledger_breaks_the_invariant():
    j = _journal_with_perp_fill(qty=0.01)
    inv = perf.order_fills_match_ledger(
        j, [{"link": "carry-x-perp-open", "status": "filled",
             "filled_qty": 0.012}])
    assert not inv["ok"]
    assert inv["detail"]["mismatched"][0]["ledger_filled_qty"] == 0.01


def test_an_order_fill_the_ledger_never_booked_breaks_it():
    import ledger as L
    inv = perf.order_fills_match_ledger(
        L.Journal(), [{"link": "carry-y", "status": "filled",
                       "filled_qty": 0.01}])
    assert not inv["ok"]


def test_unfinished_orders_are_left_to_the_unresolved_blocker():
    import ledger as L
    inv = perf.order_fills_match_ledger(
        L.Journal(), [{"link": "carry-y", "status": "partial",
                       "filled_qty": 0.01}])
    assert inv["ok"]
