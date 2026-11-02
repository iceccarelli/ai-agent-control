"""The opportunity map is read-only, offline, in-sample, and calls the production gate.

These tests pin the arithmetic on hand-built series (so a wrong sign or an
off-by-one print cannot hide in a real-data total), the fact that the gate is
CALLED rather than re-derived, and the properties the tool promises: it reads no
environment, touches no network, writes nothing unless asked, and states that the
repository holds no out-of-sample settlement.
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
sys.path.insert(0, os.path.join(BOT, "tools"))

import carry_costs  # noqa: E402
import opportunity_map as om  # noqa: E402

TOOL = os.path.join(BOT, "tools", "opportunity_map.py")
DAY = 86_400_000
T0 = 1_700_000_000_000 - (1_700_000_000_000 % (8 * 3_600_000))


def series(rates, basis=0.0, start=T0):
    """Settlements 8h apart at a flat price with a constant basis (bps)."""
    spot = 80_000.0
    return [om.Settle(start + k * 8 * 3_600_000, spot * (1 + basis / 1e4), spot, r)
            for k, r in enumerate(rates)]


@pytest.fixture(autouse=True)
def _no_ledger_writes(monkeypatch):
    # The engine-replay path loads through carry_backtest, which appends to the
    # data-read ledger when a window is new. A test must not write the repo.
    monkeypatch.setenv("LEDGER_DISABLED", "1")


class TestCostArithmetic:
    TIER = {"perp_taker_bps": 5.5, "perp_maker_bps": 2.0,
            "spot_taker_bps": 10.0, "spot_maker_bps": 10.0}

    def test_overlay_is_two_perp_legs_and_acquire_adds_the_spot_round_trip(self):
        ov = om.round_trip_bps(mode="overlay", style="taker", tier=self.TIER,
                               slippage_bps_per_leg=1.0)
        ac = om.round_trip_bps(mode="acquire", style="taker", tier=self.TIER,
                               slippage_bps_per_leg=1.0)
        assert ov["fee_bps"] == 11.0 and ov["slippage_bps"] == 2.0
        assert ac["fee_bps"] == 31.0 and ac["slippage_bps"] == 4.0
        assert ac["fee_bps"] == carry_costs.ROUND_TRIP_BPS

    def test_a_maker_round_trip_pays_no_slippage_and_the_maker_fee(self):
        mk = om.round_trip_bps(mode="overlay", style="maker", tier=self.TIER,
                               slippage_bps_per_leg=1.0)
        assert mk == {"fee_bps": 4.0, "slippage_bps": 0.0, "total_bps": 4.0}

    def test_breakeven_funding_is_cost_over_prints_plus_capital(self):
        assert om.breakeven_funding(11.0, 5.8) == pytest.approx(11.0 / 17.4)
        # 3.65% a year of capital cost is 1 bp a day = 1/3 bp per print
        assert om.breakeven_funding(0.0, 5.8, 0.0365) == pytest.approx(1.0 / 3.0)

    def test_the_tier_equals_the_config_defaults_without_the_tool_importing_config(self):
        import config   # the TEST may import it; the tool must not (see below)
        spot = config.load({"CATEGORY": "spot"})
        perp = config.load({"CATEGORY": "linear"})
        tier = om.fee_tier()
        assert (tier["perp_taker_bps"], tier["perp_maker_bps"]) == (
            perp.TAKER_FEE_BPS, perp.MAKER_FEE_BPS)
        assert (tier["spot_taker_bps"], tier["spot_maker_bps"]) == (
            spot.TAKER_FEE_BPS, spot.MAKER_FEE_BPS)


class TestTheGateIsCalledNotCopied:
    RT = 11.0

    @pytest.mark.parametrize("f,basis", [(0.05, -27.0), (0.3, -27.0), (0.5, -27.0),
                                         (0.7, -27.0), (0.3, 20.0), (-0.2, 0.0)])
    def test_reason_equals_the_production_gate(self, f, basis):
        spot = 80_000.0
        v = carry_costs.entry_gate(
            funding_prints_bps=[f] * 8, perp=spot * (1 + basis / 1e4), spot=spot,
            borrow_apr=0.0, round_trip_bps=self.RT)
        assert om.gate_reason(f, basis, rt_bps=self.RT, spot=spot) == v.reason

    def test_the_minus_27_bps_case_has_the_documented_bands(self):
        bands = om.reason_bands(-27.0, rt_bps=self.RT, step=0.002)
        by = {b["reason"]: b for b in bands}
        # net edge hits zero where 3f = RT / 30 -> f = 0.1222
        assert by["CARRY_BELOW_COST_OF_CAPITAL"]["to_bps"] == pytest.approx(0.122, abs=0.003)
        # basis budget (3f - RT/30) * 30 = 27  ->  f = (27 + 11) / 90 = 0.4222
        assert by["ENTRY_BASIS_DISCOUNT_EXCEEDS_CARRY_BUDGET"]["to_bps"] == pytest.approx(
            38.0 / 90.0, abs=0.003)
        # the hold gate: ceil(11 / f) <= 17  ->  f >= 11/17
        assert by["BREAKEVEN_HOLD_EXCEEDS_DEMONSTRATED_HOLD"]["to_bps"] == pytest.approx(
            11.0 / 17.0, abs=0.003)
        assert by["CARRY_CLEARS_COSTS_AND_BASIS"]["from_bps"] == pytest.approx(
            11.0 / 17.0, abs=0.003)

    def test_at_a_typical_basis_the_same_funding_is_refused_by_the_hold_gate_instead(self):
        # f = 0.3 bps: at -27 the NAMED reason is the basis; at -5 it is the hold.
        assert om.gate_reason(0.3, -27.0, rt_bps=self.RT) == \
            "ENTRY_BASIS_DISCOUNT_EXCEEDS_CARRY_BUDGET"
        assert om.gate_reason(0.3, -5.0, rt_bps=self.RT) == \
            "BREAKEVEN_HOLD_EXCEEDS_DEMONSTRATED_HOLD"
        # ...and 0.6 is refused at EVERY basis the hold gate decides, -27 included.
        assert om.gate_reason(0.6, -27.0, rt_bps=self.RT) == \
            "BREAKEVEN_HOLD_EXCEEDS_DEMONSTRATED_HOLD"

    def test_basis_limits_follow_the_budget_formula(self):
        lim = om.basis_limits(1.0, rt_bps=self.RT)
        # (3 * 1.0 - 11/30) * 30 = 79
        assert lim["max_premium_bps"] == pytest.approx(79.0, abs=1e-3)
        assert lim["max_discount_bps"] == pytest.approx(79.0, abs=1e-3)
        assert om.basis_limits(0.3, rt_bps=self.RT)["max_premium_bps"] is None

    def test_entry_benefit_threshold_is_cost_over_prints_plus_one_sigma(self):
        f = om.min_funding_entry_benefit(sd_bps=0.4, rt_fee_bps=11.0,
                                         spread_bps=0.0, slippage_bps=2.0)
        assert f == pytest.approx(13.0 / 17.4 + 0.4, abs=1e-6)
        # a stated cash cost raises it by the per-print cost
        g = om.min_funding_entry_benefit(sd_bps=0.4, rt_fee_bps=11.0, spread_bps=0.0,
                                         slippage_bps=2.0, opp_apr=0.0365)
        assert g == pytest.approx(f + 1.0 / 3.0, abs=1e-6)


class TestForcedHold:
    def test_funding_is_the_prints_after_entry_and_basis_is_entry_minus_exit(self):
        rows = series([9.0, 1.0, 2.0, 3.0, 4.0, 99.0])
        rows[0] = om.Settle(rows[0].ms, 80_000 * 1.0030, 80_000.0, 9.0)   # +30 bps in
        rows[3] = om.Settle(rows[3].ms, 80_000 * 1.0010, 80_000.0, 3.0)   # +10 bps out
        t = om.forced_hold(rows, 0, 3, cost_bps=5.0)
        assert t["funding_bps"] == pytest.approx(1.0 + 2.0 + 3.0)   # NOT the entry print
        assert t["basis_pnl_bps"] == pytest.approx(30.0 - 10.0, abs=1e-6)
        assert t["net_bps"] == pytest.approx(6.0 + 20.0 - 5.0, abs=1e-6)

    def test_an_exit_beyond_the_data_is_censored_not_closed_at_the_last_price(self):
        rows = series([1.0] * 5)
        assert om.forced_hold(rows, 3, 3, cost_bps=0.0) is None
        assert len(om.all_forced(rows, 3, cost_bps=0.0)) == 2

    def test_capital_cost_is_charged_per_day_held(self):
        rows = series([1.0] * 20)
        t = om.forced_hold(rows, 0, 3, cost_bps=0.0, capital_apr=0.0365)
        assert t["cost_bps"] == pytest.approx(1.0)       # 1 bp/day x 1 day

    def test_gated_sequence_never_overlaps_and_obeys_the_production_gate(self):
        rows = series([1.5] * 120)                       # rich and steady
        seq = om.gated_sequence(rows, 17, rt_gate_bps=11.0, cost_bps=11.0)
        ts = seq["trades"]
        assert len(ts) >= 5
        for a, b in zip(ts, ts[1:]):
            assert b["i"] > a["i"] + 17                  # one position at a time
        assert all(t["funding_bps"] == pytest.approx(1.5 * 17) for t in ts)
        assert seq["gate_reasons"].get("CARRY_CLEARS_COSTS_AND_BASIS", 0) >= len(ts)

    def test_a_thin_rate_never_opens(self):
        seq = om.gated_sequence(series([0.3] * 80), 17, rt_gate_bps=11.0, cost_bps=11.0)
        assert seq["trades"] == []

    def test_a_deep_discount_is_refused_even_when_funding_is_rich(self):
        # 1.0 bps steady admits |basis| <= 79; -120 must be refused
        seq = om.gated_sequence(series([1.0] * 80, basis=-120.0), 17,
                                rt_gate_bps=11.0, cost_bps=11.0)
        assert seq["trades"] == []
        assert "ENTRY_BASIS_DISCOUNT_EXCEEDS_CARRY_BUDGET" in seq["gate_reasons"]


class TestSplitsAndStatistics:
    START, END = 1_788_739_200, 1_793_491_199

    def _trade(self, ms, exit_ms, net):
        return {"ms": ms, "exit_ms": exit_ms, "net_bps": net}

    def test_straddling_and_post_window_trades_are_labelled_not_pooled(self):
        before = self._trade((self.START - 20 * 86400) * 1000, (self.START - 10 * 86400) * 1000, 1.0)
        straddle = self._trade((self.START - 1 * 86400) * 1000, (self.START + 5 * 86400) * 1000, 2.0)
        after = self._trade((self.END + 86400) * 1000, (self.END + 7 * 86400) * 1000, 3.0)
        rep = om.split_report([before, straddle, after], discovery_start=self.START,
                              discovery_end=self.END)
        groups = rep["relative_to_discovery_window"]
        assert set(groups) == {"before_window", "straddles_window_start", "after_window"}
        assert rep["out_of_sample_trades_after_window_end"] == 1

    def test_bootstrap_is_deterministic_and_refuses_too_little_data(self):
        v = [float(i % 7) for i in range(200)]
        a = om.block_bootstrap_mean(v, block=10)
        assert a == om.block_bootstrap_mean(v, block=10)
        assert a[0] <= sum(v) / len(v) <= a[1]
        assert om.block_bootstrap_mean(v[:15], block=10) is None

    def test_summary_of_nothing_is_n_zero(self):
        assert om.summarise([]) == {"n": 0}

    def test_buckets_partition_every_entry_once(self):
        entries = [{"basis_in": b, "funding_bps": 1.0, "basis_pnl_bps": 0.0, "net_bps": 0.0}
                   for b in (-20, -7, -1, 3, 8, 30)]
        table = om.bucket_table(entries, "basis_in", (-10.0, -5.0, 0.0, 5.0, 10.0))
        assert sum(r["n"] for r in table) == len(entries)
        assert [r["n"] for r in table] == [1, 1, 1, 1, 1, 1]


class TestThresholdTables:
    def test_threshold_rows_count_only_entries_at_or_above_it_by_year(self):
        y22 = 1_650_000_000_000
        y24 = 1_710_000_000_000
        e = [{"ms": y22, "ewma_bps": 0.5, "net_bps": -3.0},
             {"ms": y22, "ewma_bps": 1.2, "net_bps": 4.0},
             {"ms": y24, "ewma_bps": 1.6, "net_bps": 20.0},
             {"ms": y24, "ewma_bps": None, "net_bps": 99.0}]
        out = om.threshold_by_year(e, (1.0,))["1.0"]
        assert out["n"] == 2 and out["mean_net_bps"] == pytest.approx(12.0)
        assert out["by_year"]["2022"] == {"n": 1, "mean_net_bps": 4.0}
        assert out["by_year"]["2024"] == {"n": 1, "mean_net_bps": 20.0}

    def test_trailing_year_uses_only_the_last_365_days(self):
        last = 1_800_000_000_000
        e = [{"ms": last - 400 * DAY, "ewma_bps": 2.0, "net_bps": 50.0},
             {"ms": last - 10 * DAY, "ewma_bps": 2.0, "net_bps": -4.0},
             {"ms": last - 10 * DAY, "ewma_bps": 0.1, "net_bps": 9.0}]
        out = om.trailing_year(e, last, 0.65)
        assert out["n"] == 1 and out["mean"] == pytest.approx(-4.0)


class TestSingleBlockHarvest:
    def test_only_prints_above_the_whole_round_trip_count_and_only_their_excess(self):
        rows = series([1.0, 12.0, 15.0, 4.0])
        out = om.single_print_ceiling(rows, (13.0, 4.0))
        assert out["max_print_bps"] == 15.0
        assert out["13.0"] == {"prints_above_round_trip": 1,
                               "sum_excess_bps_over_the_whole_corpus": 2.0}
        assert out["4.0"]["prints_above_round_trip"] == 2     # strictly above; 4.0 is not
        assert out["4.0"]["sum_excess_bps_over_the_whole_corpus"] == pytest.approx(8.0 + 11.0)


class TestLot:
    TIER = TestCostArithmetic.TIER

    def test_one_lot_is_the_whole_position_and_the_cap_binds_at_100k(self):
        lot = om.lot_economics(price=80_000.0, cap_usd=100.0, lot_btc=0.001,
                               tier=self.TIER, slippage_bps_per_leg=1.0)
        assert lot["one_lot_notional_usd"] == pytest.approx(80.0)
        assert lot["lots_that_fit_under_cap"] == 1
        assert lot["price_above_which_one_lot_exceeds_cap_usd"] == pytest.approx(100_000.0)
        assert lot["round_trip_taker_usd"] == pytest.approx(80.0 * 13.0 / 1e4)
        assert lot["funding_usd_per_year_at"]["1.0"] == pytest.approx(80.0 * 1.0 * 3 * 365 / 1e4)
        too_dear = om.lot_economics(price=120_000.0, cap_usd=100.0, lot_btc=0.001,
                                    tier=self.TIER, slippage_bps_per_leg=1.0)
        assert too_dear["lots_that_fit_under_cap"] == 0 and not too_dear["fits_under_cap"]


@pytest.fixture(scope="module")
def rows():
    return om.load_bybit(BOT)


class TestOnTheRepositoryCorpus:

    def test_it_pairs_the_same_settlements_as_the_backtest_loader(self, rows):
        import carry_backtest as cb
        ref, _meta = cb.load_bybit_settlements(BOT)
        assert len(rows) == len(ref)
        for mine, theirs in ((rows[0], ref[0]), (rows[1234], ref[1234]), (rows[-1], ref[-1])):
            assert mine.ms == theirs.ms
            assert mine.perp == pytest.approx(theirs.perp)
            assert mine.spot == pytest.approx(theirs.spot)
            assert mine.rate_bps == pytest.approx(theirs.rate * 1e4)

    def test_the_repository_holds_no_settlement_after_the_discovery_window(self, rows):
        assert max(r.ms for r in rows) / 1000.0 < om.DISCOVERY_START_EPOCH + 10 * 86400
        assert not [r for r in rows if r.ms / 1000.0 > om.DISCOVERY_END_EPOCH]

    def test_inventory_reports_no_real_file_past_the_window_end(self):
        inv = om.inventory(BOT, discovery_start=om.DISCOVERY_START_EPOCH,
                           discovery_end=om.DISCOVERY_END_EPOCH)
        assert inv["real_files_with_data_after_window_end"] == 0
        assert inv["real_files"] > 20 and inv["synthetic_files"] >= 1
        assert inv["instruments_the_engine_may_trade"] == ["BTCUSDT"]
        assert all(f["synthetic"] for f in inv["files"]
                   if f["corpus"] in ("ohlcv", "orderbook", "quotes"))

    def test_the_report_is_labelled_in_sample_and_pins_the_nov2_bands(self):
        rep = om.build_report(BOT, discovery_start=om.DISCOVERY_START_EPOCH,
                              discovery_end=om.DISCOVERY_END_EPOCH,
                              slippage_bps_per_leg=1.0, with_engine=False)
        h = rep["history"]
        assert h["in_sample"] is True and h["evidence_class"] == om.HISTORICAL
        assert h["settlements_after_discovery_window_end"] == 0
        assert h["gated_sequential_taker"]["splits"]["out_of_sample_trades_after_window_end"] == 0
        bands = {b["reason"]: b for b in rep["gate"]["nov2_style_case"]["bands_at_this_basis"]}
        assert bands["ENTRY_BASIS_DISCOUNT_EXCEEDS_CARRY_BUDGET"]["from_bps"] == pytest.approx(0.124, abs=0.01)
        assert rep["gate"]["nov2_style_case"]["min_funding_to_pass_mechanical_gate_bps"] == \
            pytest.approx(11.0 / 17.0, abs=0.003)
        # a -27 bps discount is outside the whole real corpus
        assert h["basis_distribution"]["share_at_or_below_minus_27bps"] == 0.0
        # the tool states its own evidence classes; nothing is called ACTUAL or FORWARD
        text = json.dumps(rep, default=str)
        assert "ACTUAL" not in text and "FORWARD_SIMULATION" not in text

    def test_the_engine_replay_wiring_returns_both_styles(self):
        out = om.engine_replay(BOT, discovery_start=om.DISCOVERY_START_EPOCH,
                               discovery_end=om.DISCOVERY_END_EPOCH)
        for style in ("taker", "maker_first"):
            assert out[style]["trades"] > 0
            assert out[style]["trades_entered_after_window_end"] == 0
        eras = out["taker"]["trade_net_after_allocated_fees_by_era_usd"]
        # the era split must add up to the replay's own total
        assert sum(e["total"] for e in eras.values()) == pytest.approx(
            out["taker"]["net_usd"], rel=0.01, abs=50.0)


class TestTheToolStaysReadOnlyAndOffline:
    def _imports(self):
        tree = ast.parse(open(TOOL, encoding="utf-8").read())
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module.split(".")[0])
        return names

    def test_no_network_client_is_imported(self):
        assert "config" not in self._imports()     # it would load() the environment
        banned = {"socket", "urllib", "http", "requests", "httpx", "aiohttp", "websocket",
                  "websockets", "ssl", "ftplib", "smtplib", "bybit_connection",
                  "carry_broker", "trading_engine", "pybit", "ccxt"}
        assert not (self._imports() & banned)

    def test_it_reads_no_environment_and_opens_no_database(self):
        src = open(TOOL, encoding="utf-8").read()
        for needle in ("os.environ", "getenv", "sqlite3", "trading_state", "/runs/", ".env"):
            code = [ln for ln in src.splitlines()
                    if needle in ln and not ln.lstrip().startswith(("#", '"'))]
            assert not code, (needle, code)

    def test_it_writes_a_file_only_when_asked(self, tmp_path, capsys):
        out = tmp_path / "report.json"
        assert om.main(["--repo", BOT, "--json", str(out)]) == 0
        assert json.loads(out.read_text())["history"]["in_sample"] is True
        assert "no edge is claimed" in capsys.readouterr().out

    def test_a_backwards_window_is_a_usage_error(self):
        with pytest.raises(SystemExit):
            om.main(["--repo", BOT, "--discovery-start", "10", "--discovery-end", "5"])
