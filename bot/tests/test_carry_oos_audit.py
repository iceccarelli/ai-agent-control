"""The out-of-sample audit: chronological isolation, censoring, honesty.

Every series here is SYNTHETIC TEST DATA. The audit's real-corpus run is
checked only for shape and for refusing to over-claim.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from unittest import mock

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, BOT)
sys.path.insert(0, os.path.join(BOT, "tools"))

import carry_backtest as cb  # noqa: E402
import carry_costs  # noqa: E402
import carry_oos_audit as oos  # noqa: E402
import carry_replay as cr  # noqa: E402

DAY = 86_400_000
H8 = 8 * 3_600_000
T0 = 1_600_000_000_000


def settle(ms, rate, perp=30_000.0, spot=29_990.0):
    return cb.Settlement(ms=ms, perp=perp, spot=spot, rate=rate,
                         perp_high=perp * 1.001, perp_low=perp * 0.999)


def series(regimes, start=T0):
    """regimes: [(count, rate), ...] -> one print per 8h, flat prices."""
    out, ms = [], start
    for count, rate in regimes:
        for _ in range(count):
            out.append(settle(ms, rate))
            ms += H8
    return out


def trading_series(cycles=40):
    """Positive funding long enough to enter, then an inversion to exit."""
    return series([(25, 0.0003), (5, -0.0004)] * cycles)


@pytest.fixture(autouse=True)
def quiet():
    import logging
    logging.getLogger("carry_engine").setLevel(logging.ERROR)


@pytest.fixture
def small_protocol():
    """A short protocol so synthetic series stay small. Test config only."""
    with mock.patch.dict(oos.PROTOCOL, {"warmup_days": 20, "folds": 3,
                                         "min_completed_per_fold": 4,
                                         "min_adequate_folds": 2,
                                         "min_total_completed": 8}):
        yield


# -- corpus and boundaries -------------------------------------------------

class TestCorpusAndBoundaries:
    def test_duplicates_out_of_order_and_non_finite_rows_refuse_the_run(self):
        rows = series([(10, 0.0001)])
        with pytest.raises(oos.CorpusError):
            oos.validate_corpus(rows + [rows[-1]])
        with pytest.raises(oos.CorpusError):
            oos.validate_corpus([rows[1], rows[0]])
        with pytest.raises(oos.CorpusError):
            oos.validate_corpus([rows[0], settle(rows[0].ms + 1, float("nan"))])
        with pytest.raises(oos.CorpusError):
            oos.validate_corpus(rows[:1])
        with pytest.raises(oos.CorpusError):
            oos.run_oos(rows + [rows[-1]])

    def test_boundaries_depend_on_timestamps_only(self):
        a = series([(400, 0.0003)])
        b = series([(400, -0.0009)])                     # opposite outcomes
        assert oos.fold_boundaries(a[0].ms, a[-1].ms) == \
            oos.fold_boundaries(b[0].ms, b[-1].ms)

    def test_blocks_are_contiguous_disjoint_and_start_after_the_warmup(self):
        warm_end, blocks = oos.fold_boundaries(T0, T0 + 400 * DAY)
        assert warm_end == T0 + oos.PROTOCOL["warmup_days"] * DAY
        assert len(blocks) == oos.PROTOCOL["folds"]
        assert blocks[0][0] == warm_end and blocks[-1][1] == T0 + 400 * DAY + 1
        for (a1, b1), (a2, b2) in zip(blocks, blocks[1:]):
            assert b1 == a2 and a1 < b1

    def test_a_corpus_shorter_than_the_warmup_has_no_test_interval(self):
        assert oos.fold_boundaries(T0, T0 + 10 * DAY)[1] == []
        rep = oos.run_oos(series([(20, 0.0003)]))
        assert rep["verdict"] == "INCONCLUSIVE"
        assert rep["verdict_reasons"] == ["NO_TEST_INTERVAL_AFTER_WARMUP"]
        assert rep["folds"] == []

    def test_there_is_no_search_over_boundaries(self):
        src = open(oos.__file__).read()
        for banned in ("shuffle", "random.sample", "argmax", "max(folds",
                       "best_"):
            assert banned not in src


# -- chronological isolation ------------------------------------------------

class TestNoFutureInformation:
    def test_an_earlier_decision_is_unchanged_by_later_data(self):
        rows = trading_series(6)
        n = 120
        t1, t2 = [], []
        cr.replay(rows[:n], notional=100_000.0, borrow_apr=0.0, trace=t1)
        wild = rows[:n] + [settle(rows[n - 1].ms + (k + 1) * H8, -0.05 + 0.1 * (k % 2),
                                  perp=1.0 + k, spot=1.0 + k)
                           for k in range(60)]
        cr.replay(wild, notional=100_000.0, borrow_apr=0.0, trace=t2)
        assert t1 and t2[:n] == t1                       # decision by decision

    def test_a_block_result_does_not_depend_on_anything_after_it(self, small_protocol):
        rows = trading_series(10)
        warm_end, blocks = oos.fold_boundaries(rows[0].ms, rows[-1].ms)
        a, b = blocks[0]
        base = oos.evaluate_fold(rows, a, b, impact_bps=0.0, cont_trades=[])
        future = [r for r in rows if r.ms >= b]
        poisoned = [r for r in rows if r.ms < b] + [
            settle(r.ms, 0.04, perp=5.0, spot=9.0) for r in future]
        again = oos.evaluate_fold(poisoned, a, b, impact_bps=0.0,
                                  cont_trades=[])
        assert json.dumps(base, sort_keys=True, default=str) == \
            json.dumps(again, sort_keys=True, default=str)

    def test_the_seed_prints_all_precede_the_block(self, small_protocol):
        rows = trading_series(10)
        _w, blocks = oos.fold_boundaries(rows[0].ms, rows[-1].ms)
        a, b = blocks[1]
        out = oos.evaluate_fold(rows, a, b, impact_bps=0.0, cont_trades=[])
        assert out["warmup_prints_used"] == 8
        assert out["warmup_prints_latest_ms"] < a

    def test_the_engine_sees_only_the_blocks_own_prints(self, small_protocol):
        rows = trading_series(10)
        _w, blocks = oos.fold_boundaries(rows[0].ms, rows[-1].ms)
        a, b = blocks[1]
        trace = []
        oos.evaluate_fold(rows, a, b, impact_bps=0.0, cont_trades=[],
                          trace=trace)
        # evaluate_fold passes its own trace into replay only when asked; the
        # replay it drives walks exactly the block's prints
        seen = []
        real = cr.replay

        def spy(test, **kw):
            seen.extend(r.ms for r in test)
            return real(test, **kw)
        with mock.patch.object(cr, "replay", spy):
            oos.evaluate_fold(rows, a, b, impact_bps=0.0, cont_trades=[])
        assert seen and min(seen) >= a and max(seen) < b

    def test_warmup_data_never_produces_a_test_trade(self, small_protocol):
        rows = trading_series(10)
        rep = oos.run_oos(rows, counterfactuals=False)
        warm_end = rep["warmup"]["end_ms"]
        for f in rep["folds"]:
            if f["completed_first_open_ms"] is not None:
                assert f["completed_first_open_ms"] >= f["start_ms"] >= warm_end


# -- production engine, frozen assumptions ----------------------------------

class TestProductionEngineAndFrozenAssumptions:
    def test_the_real_engine_and_gate_decide_every_print(self, small_protocol):
        from carry_engine import CarryEngine
        rows = trading_series(10)
        calls = {"candle": 0, "gate": 0}
        real_candle = CarryEngine.on_candle
        real_gate = carry_costs.evaluate_entry

        def candle(self, **kw):
            calls["candle"] += 1
            return real_candle(self, **kw)

        def gate(**kw):
            calls["gate"] += 1
            return real_gate(**kw)
        with mock.patch.object(CarryEngine, "on_candle", candle), \
                mock.patch.object(carry_costs, "evaluate_entry", gate):
            oos.run_oos(rows, counterfactuals=False)
        assert calls["candle"] > len(rows) and calls["gate"] > 0

    def test_the_tool_contains_no_copy_of_the_gate_or_engine_rules(self):
        tree = ast.parse(open(oos.__file__).read())
        names = {n.name for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
        assert not names & {"evaluate_entry", "ewma_funding_bps", "on_candle",
                            "may_open_gated", "CarryEngine"}

    def test_the_30_day_assumption_is_frozen_and_untouched_by_counterfactuals(
            self, small_protocol):
        original = carry_costs.evaluate_entry
        rep = oos.run_oos(trading_series(10))
        assert carry_costs.ASSUMED_HOLD_DAYS == 30.0
        assert carry_costs.evaluate_entry is original
        assert rep["frozen_production"]["ASSUMED_HOLD_DAYS"] == 30.0
        assert [c["assumed_hold_days"] for c in rep["counterfactuals"]] == \
            oos.PROTOCOL["counterfactual_hold_days"]
        assert all(c["label"] == "DIAGNOSTIC_COUNTERFACTUAL" and
                   c["production_value_unchanged"] == 30.0
                   for c in rep["counterfactuals"])

    def test_counterfactuals_cannot_influence_or_rank_the_verdict(
            self, small_protocol):
        rows = trading_series(10)
        with_cf = oos.run_oos(rows)
        without = oos.run_oos(rows, counterfactuals=False)
        assert with_cf["verdict"] == without["verdict"]
        assert with_cf["folds"] == without["folds"]
        blob = json.dumps(with_cf)
        for banned in ("best", "winner", "recommended", "optimal"):
            assert banned not in blob.lower().replace(
                "not a recommendation", "")

    def test_the_hold_horizon_context_changes_the_gate_and_restores_it(self):
        seen = []
        real = carry_costs.evaluate_entry

        def spy(**kw):
            seen.append(kw.get("hold_days"))
            return real(**kw)
        with mock.patch.object(carry_costs, "evaluate_entry", spy):
            with oos.hold_horizon(7.0):
                carry_costs.evaluate_entry(
                    funding_prints_bps=[3.0] * 8, perp=30_000.0, spot=29_990.0,
                    borrow_apr=0.0, round_trip_bps=11.0)
            assert carry_costs.evaluate_entry is spy   # restored on exit
        assert seen == [7.0]
        assert carry_costs.ASSUMED_HOLD_DAYS == 30.0


# -- boundary positions ------------------------------------------------------

class TestBoundaryPositions:
    def test_a_position_open_at_a_block_end_is_censored_not_an_engine_exit(
            self, small_protocol):
        # negative through the warm-up and the start of block 1, then positive
        # for good: the position opens INSIDE block 1 and is still open at its end
        rows = series([(70, -0.0003), (330, 0.0003)])
        rep = oos.run_oos(rows, counterfactuals=False)
        for f in rep["folds"]:
            assert f["engine_exit_reasons"] == {}       # nothing ever exits
            assert f["completed"] == 0 and f["censored"] <= 1
            assert f["gross_modelled"] is None          # nothing realised
        assert rep["folds"][0]["censored"] == 1
        c = [x for f in rep["folds"] for x in f["censored_exposure"]][0]
        assert "not an engine exit" in c["note"]

    def test_a_continuous_position_across_a_boundary_is_not_a_new_test_trade(
            self, small_protocol):
        rows = series([(400, 0.0003)])
        rep = oos.run_oos(rows, counterfactuals=False)
        later = [f for f in rep["folds"][1:]]
        assert later and all(f["purged"] == 1 for f in later)
        assert all(f["completed"] == 0 for f in later)
        assert all(f["purged_boundary_reentries"][0]["opened_ms"]
                   == f["test_start_first_print_ms"] for f in later)
        assert all(f["censored"] == 0 for f in later)    # purged, not exposure

    def test_no_purge_when_no_position_spans_the_boundary(self, small_protocol):
        rows = trading_series(12)
        _w, blocks = oos.fold_boundaries(rows[0].ms, rows[-1].ms)
        a, b = blocks[0]
        out = oos.evaluate_fold(rows, a, b, impact_bps=0.0, cont_trades=[])
        assert out["purged"] == 0
        assert out["pre_boundary_position_in_continuous_run"] is False


# -- honest economics -------------------------------------------------------

class TestEconomicsAreHonest:
    def test_a_no_trade_period_reports_nothing_not_zero(self, small_protocol):
        rows = series([(400, -0.0003)])
        rep = oos.run_oos(rows, counterfactuals=False)
        for f in rep["folds"]:
            assert f["completed"] == 0 and f["entries"] == 0
            assert f["stats"]["mean"] is None and f["stats"]["median"] is None
            assert f["net_annualised_pct"] is None
            assert f["hold_days"] is None
            assert f["refusals"]
        assert rep["combined"]["net_annualised_pct"] is None
        assert rep["combined"]["gross_modelled"] is None
        assert rep["verdict"] == "INCONCLUSIVE"

    def test_a_missing_cost_is_reported_unavailable_not_zero(self, small_protocol):
        rep = oos.run_oos(trading_series(10), counterfactuals=False)
        costs = rep["folds"][0]["costs"]
        assert costs["borrow"]["value_usd"] is None
        assert "not a measured" in costs["borrow"]["reason"]
        assert costs["not_modelled"]

    def test_fold_results_carry_sample_size_exposure_and_uncertainty(
            self, small_protocol):
        rep = oos.run_oos(trading_series(12), counterfactuals=False)
        f = next(x for x in rep["folds"] if x["completed"] >= 3)
        for key in ("entries", "completed", "censored", "purged", "refusals",
                    "engine_exit_reasons", "hold_days", "gross_modelled",
                    "gate_predicted_funding_usd", "funding_earned_usd",
                    "realised_trade_drawdown_usd",
                    "market_exposure_pct_incl_censored", "stats"):
            assert key in f
        assert f["stats"]["n"] == f["completed"]
        assert f["stats"]["sem"] is not None
        assert set(f["gross_modelled"]) == {"funding", "basis", "fees", "net"}

    def test_the_bootstrap_is_deterministic(self):
        v = [3.0, -1.0, 4.0, -2.0, 5.0, -9.0, 7.0]
        a = oos.bootstrap_mean_ci(v, resamples=500, seed=1, level=0.95)
        b = oos.bootstrap_mean_ci(v, resamples=500, seed=1, level=0.95)
        assert a == b and a[0] < a[1]
        assert oos.bootstrap_mean_ci([1.0], resamples=10, seed=1, level=0.9) is None


# -- the verdict cannot overclaim -------------------------------------------

class TestVerdictCannotOverclaim:
    def folds(self, completed, net=10.0, n=4):
        return [{"status": "EVALUATED", "completed": completed,
                 "adequate": completed >= oos.PROTOCOL["min_completed_per_fold"],
                 "gross_modelled": {"net": net}} for _ in range(n)]

    def test_few_trades_are_inconclusive_whatever_they_earned(self):
        f = self.folds(5)
        v, why = oos._verdict([(0, 1)] * 4, f, 0, 20, [100.0] * 20,
                              (50.0, 150.0), f)
        assert v == "INCONCLUSIVE" and "INSUFFICIENT_SAMPLE" in why

    def test_the_total_sample_floor_is_enforced_on_its_own(self):
        f = self.folds(25)
        v, why = oos._verdict([(0, 1)] * 4, f, 4, 59, [1.0] * 59,
                              (0.5, 2.0), f)
        assert v == "INCONCLUSIVE"
        assert any(w.startswith("TOTAL_COMPLETED_59") for w in why)

    def test_a_small_adequate_set_with_a_wide_interval_is_inconclusive(self):
        f = self.folds(25)
        v, _ = oos._verdict([(0, 1)] * 4, f, 4, 100, [1.0] * 100,
                            (-10.0, 50.0), f)
        assert v == "INCONCLUSIVE"

    def test_the_best_possible_verdict_still_says_not_independent(self):
        f = self.folds(25)
        v, why = oos._verdict([(0, 1)] * 4, f, 4, 100, [1.0] * 100,
                              (0.5, 2.0), f)
        assert v == "CHRONOLOGICALLY_CONSISTENT_NOT_INDEPENDENT"
        assert "PARAMETERS_NOT_INDEPENDENT_OF_CORPUS" in why

    def test_a_negative_interval_is_reported_negative(self):
        f = self.folds(25, net=-5.0)
        v, _ = oos._verdict([(0, 1)] * 4, f, 4, 100, [-1.0] * 100,
                            (-2.0, -0.5), [])
        assert v == "NEGATIVE_OUT_OF_SAMPLE"

    def test_no_verdict_label_claims_an_edge_or_independent_validation(self):
        for label in oos.VERDICTS:
            assert not any(w in label for w in ("PROFIT", "EDGE", "ESTABLISHED",
                                                "VALIDATED", "PROVEN"))


# -- report, provenance, isolation -----------------------------------------

class TestReportAndIsolation:
    def test_the_report_is_valid_json_reproducible_and_labelled_modelled(
            self, small_protocol):
        rows = trading_series(10)
        a = oos.run_oos(rows, meta={"corpus_sha256": "x"})
        b = oos.run_oos(rows, meta={"corpus_sha256": "x"})
        sa = json.dumps(a, sort_keys=True, allow_nan=False)
        assert sa == json.dumps(b, sort_keys=True, allow_nan=False)
        assert a["schema"] == oos.SCHEMA
        assert a["evidence_class"] == "MODELLED_HISTORICAL_SIMULATION"
        assert a["venue_observed_pnl"] is None
        for key in ("protocol", "protocol_id", "corpus", "frozen_production",
                    "warmup", "folds", "combined", "verdict", "limitations",
                    "independence", "counterfactuals"):
            assert key in a
        assert a["corpus"]["records"] == len(rows)
        assert a["protocol_id"] == oos.protocol_id()

    def test_the_real_corpus_run_is_shaped_and_does_not_overclaim(self, tmp_path):
        rep = oos.build_report(BOT)
        assert rep["corpus"]["corpus_sha256"] and rep["corpus"]["records"] > 4000
        assert rep["code"]["git_rev"]
        assert rep["verdict"] in oos.VERDICTS
        assert rep["frozen_production"]["ASSUMED_HOLD_DAYS"] == 30.0
        assert len(rep["folds"]) == oos.PROTOCOL["folds"]
        if rep["combined"]["adequate_folds"] < oos.PROTOCOL["min_adequate_folds"]:
            assert rep["verdict"] == "INCONCLUSIVE"

    def test_atomic_write_leaves_nothing_on_a_failed_serialisation(self, tmp_path):
        target = tmp_path / "r.json"
        with pytest.raises(ValueError):
            oos.write_atomic(str(target), {"x": float("nan")})
        assert list(tmp_path.iterdir()) == []
        oos.write_atomic(str(target), {"x": 1})
        assert json.loads(target.read_text()) == {"x": 1}
        assert [p.name for p in tmp_path.iterdir()] == ["r.json"]

    def test_an_invalid_corpus_writes_no_report(self, tmp_path, monkeypatch):
        def bad(repo):
            raise oos.CorpusError("duplicate")
        monkeypatch.setattr(oos, "build_report", bad)
        out = tmp_path / "r.json"
        assert oos.main(["--repo", str(tmp_path), "--json", str(out)]) == 2
        assert not out.exists()

    def test_the_tool_touches_no_ledger_database_network_or_order_path(
            self, tmp_path, monkeypatch, small_protocol):
        tree = ast.parse(open(oos.__file__).read())
        imported = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                imported |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom):
                imported.add((n.module or "").split(".")[0])
        assert not imported & {"persistence", "sqlite3", "requests", "urllib",
                               "socket", "http", "bybit_connection",
                               "carry_broker", "main", "websocket"}
        src = open(oos.__file__).read()
        for banned in ("save_ledger", "StateStore", "place_market",
                       "place_post_only", "LIVE_TRADING_ACK", "PAPER_TRADING"):
            assert banned not in src
        monkeypatch.chdir(tmp_path)
        oos.run_oos(trading_series(8), counterfactuals=False)
        assert list(tmp_path.iterdir()) == []            # no db, no artefact

    def test_simulated_cashflows_do_not_reach_a_live_journal(self, small_protocol):
        import ledger as L
        live = L.Journal()
        before = list(live.entries)
        oos.run_oos(trading_series(8), counterfactuals=False)
        assert live.entries == before                    # replay uses its own


def test_cli_runs_end_to_end_and_writes_an_identical_report_twice(tmp_path):
    out1, out2 = tmp_path / "a.json", tmp_path / "b.json"
    for out in (out1, out2):
        r = subprocess.run(
            [sys.executable, os.path.join(BOT, "tools", "carry_oos_audit.py"),
             "--repo", BOT, "--json", str(out)],
            capture_output=True, text=True, timeout=300,
            env={k: v for k, v in os.environ.items()
                 if k not in ("LIVE_TRADING_ACK",)})
        assert r.returncode == 0, r.stderr[-400:]
        assert "MODELLED only" in r.stdout
    assert out1.read_bytes() == out2.read_bytes()
    assert json.loads(out1.read_text())["schema"] == oos.SCHEMA
