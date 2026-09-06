"""Slice 32 — the contract that stops Stage 1 running on the wrong files.

The failure mode has a name: *"we ran Stage 1 on whatever CSV was in the folder."*

It is not hypothetical. `data/ohlcv/` holds `BYBIT_SPOT_ETH_USDT_1H.csv.gz` and
`BYBIT_SPOT_SOL_USDT_1H.csv.gz` — 5,000 bars each, correct headers, plausible
prices, and entirely generated. A future session looking for multi-asset data
finds them first.

So the tests here are mostly about what the contract must **refuse**, and about
the report saying things out loud rather than leaving them to be inferred.
"""
from __future__ import annotations

import ast
import gzip
import inspect
import json
import os
import sys

import pytest

import registration_invariant

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tools"))

import data_contract as dc  # noqa: E402

HEADER = ("time_period_start,time_period_end,time_open,time_close,price_open,"
          "price_high,price_low,price_close,volume_traded,trades_count")

_EPOCH = __import__("datetime").datetime(2020, 1, 1)


def _stamp(index, bar_seconds=86400, suffix="Z"):
    """Bar `index` of a regular series, in the corpus's timestamp format."""
    import datetime as dt
    moment = _EPOCH + dt.timedelta(seconds=index * bar_seconds)
    return moment.strftime("%Y-%m-%dT%H:%M:%S") + ".0000000" + suffix


def make_corpus(root, name, *, synthetic, bar_seconds=86400, rows=600,
                exchange="BITSTAMP", symbol="BITSTAMP_SPOT_BTC_USD_1D",
                duplicate=False, out_of_order=False, bad_header=False,
                manifest=True):
    """Build a corpus on disk with exactly the properties a test needs."""
    directory = os.path.join(str(root), name)
    ohlcv = os.path.join(directory, "ohlcv")
    os.makedirs(ohlcv, exist_ok=True)

    if manifest:
        payload = {"synthetic": synthetic, "bar_seconds": bar_seconds}
        if exchange:
            payload["source"] = {"exchange": exchange}
        with open(os.path.join(directory, "MANIFEST.json"), "w") as handle:
            json.dump(payload, handle)

    header = "a,b,c" if bad_header else HEADER
    lines = [header]
    # Rows the real loader accepts: strictly increasing, period end after
    # period start, trade times inside the period. Since slice 38 the contract
    # asks `market_data.load_ohlcv` whether a corpus is readable, so a fixture
    # that is only *shaped* like a corpus would be refused for the right
    # reason and fail the test for the wrong one.
    for i in range(rows):
        start = _stamp(i, bar_seconds)
        end = _stamp(i + 1, bar_seconds)
        lines.append(f"{start},{end},{start},{end},1,2,0.5,1.5,10,3")
    if duplicate and rows >= 2:
        lines[2] = lines[1]
    if out_of_order and rows >= 3:
        lines[1], lines[2] = lines[2], lines[1]

    with gzip.open(os.path.join(ohlcv, f"{symbol}.csv.gz"), "wt") as handle:
        handle.write("\n".join(lines) + "\n")
    return directory


class TestKnownCorporaAreClassified:
    """The real repository, scanned. No crash, correct labels."""

    def test_the_scan_completes(self):
        records = dc.scan_all(repo_root=REPO)
        assert records
        assert all(isinstance(r, dc.CorpusRecord) for r in records)

    @pytest.mark.parametrize("path,interval", [
        ("data/real_1d", "D"), ("data/real_4h", "4H"), ("data/real", "1H"),
    ])
    def test_the_real_btc_corpora_are_eligible(self, path, interval):
        record = dc.scan_corpus(path, repo_root=REPO)
        assert record.exists is True
        assert record.is_synthetic is False
        assert record.is_real_exchange_ohlcv is True
        assert record.interval_label == interval
        assert record.eligible_for_stage1 is True
        assert record.ineligible_reasons == []
        assert record.symbols == ["BTC_USD"]

    def test_bar_counts_match_the_corpora_used_in_edge_md(self):
        """3,135 / 15,379 / 61,513 — the numbers quoted throughout EDGE.md.

        `data/real_1d` was 2,564 bars up to and including slice 37 and is 3,135
        from slice 38, when the human shipped the Bitstamp daily extension
        (2025-01-08 .. 2026-08-01, 571 bars). The count is asserted exactly, not
        as a lower bound: a corpus that grows without anyone noticing is the
        same problem as one that shrinks.
        """
        counts = {p: dc.scan_corpus(p, repo_root=REPO).bar_count
                  for p in ("data/real_1d", "data/real_4h", "data/real")}
        assert counts["data/real_1d"] == 3135
        assert counts["data/real_4h"] == 15379
        assert counts["data/real"] == 61513

    def test_the_daily_corpus_spans_the_dates_the_measurement_claims(self):
        """A row count alone cannot tell you *which* 3,135 days you have."""
        import market_data
        import datetime as dt
        path = os.path.join(REPO, "data/real_1d/ohlcv/"
                                  "BITSTAMP_SPOT_BTC_USD_1D.csv.gz")
        bars = market_data.load_ohlcv(path).bars
        day = lambda b: dt.datetime.utcfromtimestamp(b.start_ms / 1000).date()
        assert str(day(bars[0])) == "2018-01-01"
        assert str(day(bars[-1])) == "2026-08-01"
        assert len(bars) == 3135

    def test_every_bar_in_the_daily_corpus_actually_loads(self):
        """The slice-38 defect, asserted directly.

        3,135 rows in the file and 2,564 bars out of the loader is a corpus
        that reports full coverage and measures a truncated window. Both
        numbers, and their agreement, are now part of the contract.
        """
        record = dc.scan_corpus("data/real_1d", repo_root=REPO)
        assert record.rejected_rows == 0
        assert record.loadable_bar_count == record.bar_count == 3135

    def test_an_eligible_corpus_carries_no_reasons(self):
        for record in dc.scan_all(repo_root=REPO):
            if record.eligible_for_stage1:
                assert record.ineligible_reasons == [], record.path

    def test_an_ineligible_corpus_always_carries_a_reason(self):
        """Silence is not an answer."""
        for record in dc.scan_all(repo_root=REPO):
            if not record.eligible_for_stage1:
                assert record.ineligible_reasons, record.path


class TestTheRealMultiAssetCorpora:
    """Slice 34 — real Binance ETH/SOL, registered as DATA ONLY.

    Their bar counts and date ranges are pinned so that a future edit which
    silently truncates, re-fetches, or swaps a corpus fails here rather than
    inside a measurement.
    """

    @pytest.mark.parametrize("path,interval,bars", [
        ("data/real_multi_1d", "D", 1461),
        ("data/real_multi_1h", "1H", 35063),
    ])
    def test_each_corpus_is_eligible_with_the_expected_shape(self, path, interval, bars):
        record = dc.scan_corpus(path, repo_root=REPO)
        assert record.exists is True
        assert record.is_synthetic is False
        assert record.is_real_exchange_ohlcv is True
        assert record.interval_label == interval
        assert record.bar_count == bars
        assert record.required_columns_ok is True
        assert record.gap_policy_result == "pass"
        assert record.timestamp_policy == "UTC"
        assert record.eligible_for_stage1 is True
        assert record.ineligible_reasons == []
        # Slice 35 added BNB and AVAX to the same corpora. The durable property
        # is that the pre-declared pair is present and every symbol is a real
        # non-BTC instrument -- not that the list is exactly two names, which
        # was a fact about slice 34 rather than about the contract.
        assert {"ETH_USDT", "SOL_USDT"} <= set(record.symbols)
        assert all("BTC" not in sym.upper() for sym in record.symbols)

    @pytest.mark.parametrize("path", ["data/real_multi_1d", "data/real_multi_1h"])
    def test_the_manifest_declares_binance_spot_provenance(self, path):
        with open(os.path.join(REPO, path, "MANIFEST.json"), encoding="utf-8") as h:
            manifest = json.load(h)
        assert manifest["synthetic"] is False
        assert manifest["source"]["exchange"] == "BINANCE"
        assert manifest["source"]["market"] == "SPOT"

    @pytest.mark.parametrize("path", ["data/real_multi_1d", "data/real_multi_1h"])
    def test_each_corpus_has_its_own_manifest(self, path):
        """Not inherited from `data/MANIFEST.json`, which says synthetic: true.

        If one of these ever lost its manifest it would inherit the synthetic
        flag from the parent and silently become ineligible — a safe direction,
        but one worth failing loudly instead.
        """
        assert os.path.isfile(os.path.join(REPO, path, "MANIFEST.json"))

    def test_the_daily_and_hourly_corpora_cover_the_same_window(self):
        daily = dc.scan_corpus("data/real_multi_1d", repo_root=REPO)
        hourly = dc.scan_corpus("data/real_multi_1h", repo_root=REPO)
        assert daily.symbols == hourly.symbols
        assert {"ETH_USDT", "SOL_USDT"} <= set(daily.symbols)
        # 1,461 days is four years; 35,063 hours is the same span less the
        # single exchange-halt hour on 2023-03-24.
        assert abs(daily.bar_count * 24 - hourly.bar_count) <= 25

    def test_registration_did_not_clear_any_signal(self):
        """The sentence EDGE.md §15b promised, asserted."""
        import project_status as ps
        status = ps.current(None)
        assert status.timing_skill_research == "CLOSED"
        assert status.cleared_edge_signal == \
            registration_invariant.stock_cleared_edge_signal()
        registration_invariant.assert_registration_is_sound(
            os.path.join(REPO, "artifacts"))


class TestSyntheticIsNeverEligible:

    def test_the_shipped_synthetic_corpus_is_refused(self):
        record = dc.scan_corpus("data/ohlcv", repo_root=REPO)
        assert record.is_synthetic is True
        assert record.eligible_for_stage1 is False
        assert any("SYNTHETIC" in r for r in record.ineligible_reasons)

    def test_it_inherits_provenance_from_the_parent_manifest(self):
        """`data/ohlcv` has no manifest of its own; `data/MANIFEST.json` governs.

        Without the parent lookup this corpus reports `is_synthetic: false` and
        the report's synthetic-symbol summary comes back empty — so it would
        fail to say that the ETH and SOL files are generated. That is the single
        most important sentence the report exists to print.
        """
        assert not os.path.exists(os.path.join(REPO, "data", "ohlcv", "MANIFEST.json"))
        assert dc.scan_corpus("data/ohlcv", repo_root=REPO).is_synthetic is True

    def test_synthetic_wins_even_when_everything_else_passes(self, tmp_path):
        """The decisive test. Every other field forced to a passing value."""
        make_corpus(tmp_path, "fake", synthetic=True, rows=5000,
                    bar_seconds=86400, exchange="BITSTAMP")
        record = dc.scan_corpus("fake", repo_root=str(tmp_path))
        assert record.required_columns_ok is True
        assert record.gap_policy_result == "pass"
        assert record.interval_label == "D"
        assert record.bar_count >= dc.MIN_BARS_DAILY
        assert record.eligible_for_stage1 is False
        assert any("SYNTHETIC" in r for r in record.ineligible_reasons)

    def test_a_real_control_corpus_is_eligible(self, tmp_path):
        """Without this, "synthetic is refused" could mean "everything is"."""
        make_corpus(tmp_path, "genuine", synthetic=False, rows=600)
        assert dc.scan_corpus("genuine", repo_root=str(tmp_path)).eligible_for_stage1


class TestMissingPathsFailClosed:

    def test_an_absent_directory_is_explicit(self, tmp_path):
        record = dc.scan_corpus("nope", repo_root=str(tmp_path))
        assert record.exists is False
        assert record.eligible_for_stage1 is False
        assert record.ineligible_reasons

    def test_no_manifest_means_no_provenance(self, tmp_path):
        make_corpus(tmp_path, "bare", synthetic=False, manifest=False)
        record = dc.scan_corpus("bare", repo_root=str(tmp_path))
        assert record.eligible_for_stage1 is False
        assert any("MANIFEST" in r for r in record.ineligible_reasons)

    def test_a_corrupt_manifest_does_not_raise(self, tmp_path):
        directory = make_corpus(tmp_path, "corrupt", synthetic=False)
        with open(os.path.join(directory, "MANIFEST.json"), "w") as handle:
            handle.write("{not json")
        record = dc.scan_corpus("corrupt", repo_root=str(tmp_path))
        assert record.eligible_for_stage1 is False

    def test_missing_columns_are_refused(self, tmp_path):
        make_corpus(tmp_path, "badcols", synthetic=False, bad_header=True)
        record = dc.scan_corpus("badcols", repo_root=str(tmp_path))
        assert record.required_columns_ok is False
        assert record.eligible_for_stage1 is False

    def test_too_few_bars_is_refused_with_the_numbers(self, tmp_path):
        make_corpus(tmp_path, "short", synthetic=False, rows=10)
        record = dc.scan_corpus("short", repo_root=str(tmp_path))
        assert record.eligible_for_stage1 is False
        assert any("10 bars" in r and "500" in r for r in record.ineligible_reasons)

    def test_a_duplicate_timestamp_fails_the_gap_policy(self, tmp_path):
        make_corpus(tmp_path, "dupes", synthetic=False, rows=600, duplicate=True)
        record = dc.scan_corpus("dupes", repo_root=str(tmp_path))
        assert record.gap_policy_result == "fail"
        assert record.eligible_for_stage1 is False

    def test_out_of_order_timestamps_fail_the_gap_policy(self, tmp_path):
        make_corpus(tmp_path, "unsorted", synthetic=False, rows=600,
                    out_of_order=True)
        record = dc.scan_corpus("unsorted", repo_root=str(tmp_path))
        assert record.gap_policy_result == "fail"
        assert record.eligible_for_stage1 is False

    def test_an_unknown_interval_is_refused(self, tmp_path):
        make_corpus(tmp_path, "weird", synthetic=False, bar_seconds=4242, rows=5000)
        record = dc.scan_corpus("weird", repo_root=str(tmp_path))
        assert record.interval_label == "unknown"
        assert record.eligible_for_stage1 is False

    def test_no_source_exchange_is_refused(self, tmp_path):
        make_corpus(tmp_path, "anon", synthetic=False, exchange="", rows=600)
        record = dc.scan_corpus("anon", repo_root=str(tmp_path))
        assert record.is_real_exchange_ohlcv is False
        assert record.eligible_for_stage1 is False


class TestNoFalseMultiAssetClaim:
    """The report must not let anyone conclude ETH/SOL coverage."""

    def test_real_multi_asset_data_is_now_present_and_named(self):
        """Slice 34: real Binance ETH/SOL arrived, so this flipped.

        The assertion is deliberately about the CONTRACT's classification, not
        about the state of the world. Its predecessor asserted
        `multi_asset_corpus_present is False`, which was a fact about a moment
        rather than a property of the code, and it broke the day real data
        landed. What must stay true is that the flag tracks *eligible real
        non-BTC corpora* — and nothing else.
        """
        report = dc.build_report(repo_root=REPO)
        assert report["multi_asset_corpus_present"] is True
        assert {"ETH_USDT", "SOL_USDT"} <= set(report["non_btc_real_symbols"])

    def test_the_flag_tracks_eligible_real_non_btc_corpora_only(self):
        """The invariant behind the flag, stated independently of today's data."""
        report = dc.build_report(repo_root=REPO)
        eligible_non_btc = {
            symbol
            for record in report["corpora"] if record["eligible_for_stage1"]
            for symbol in record["symbols"]
            if "BTC" not in symbol.upper()
        }
        assert report["multi_asset_corpus_present"] is bool(eligible_non_btc)
        assert set(report["non_btc_real_symbols"]) == eligible_non_btc

    def test_no_synthetic_corpus_contributes_to_the_flag(self):
        """The decisive one: generated ETH/SOL must not be able to set it."""
        report = dc.build_report(repo_root=REPO)
        for record in report["corpora"]:
            if record["is_synthetic"]:
                assert record["eligible_for_stage1"] is False, record["path"]
                for symbol in record["symbols"]:
                    assert record["path"] not in \
                        report["non_btc_real_symbols_by_corpus"], record["path"]

    def test_the_synthetic_eth_and_sol_are_still_named_as_synthetic(self):
        """Unchanged by slice 34: the generated pair is still called generated."""
        report = dc.build_report(repo_root=REPO)
        synthetic = report["non_btc_synthetic_symbols_present"]
        assert "ETH_USDT" in synthetic
        assert "SOL_USDT" in synthetic

    def test_they_are_not_in_the_eligible_list(self):
        report = dc.build_report(repo_root=REPO)
        assert "data/ohlcv" not in report["stage1_eligible_corpus_paths"]

    def test_every_eligible_corpus_is_real_exchange_data(self):
        """Replaces an assertion that every eligible corpus was BTC.

        That was true until slice 34 and is no longer. The durable property is
        that eligibility requires established real-exchange provenance — which
        is what the contract is for.
        """
        report = dc.build_report(repo_root=REPO)
        for record in report["corpora"]:
            if record["eligible_for_stage1"]:
                assert record["is_real_exchange_ohlcv"] is True, record["path"]
                assert record["is_synthetic"] is False, record["path"]

    def test_the_real_and_synthetic_eth_are_distinguishable_by_path(self):
        """`ETH_USDT` now names a real file AND a generated one.

        A report that listed only symbol names would leave a reader unable to
        tell which is which — the precise ambiguity that leads to measuring a
        generator. The by-corpus maps exist for this.
        """
        report = dc.build_report(repo_root=REPO)
        real = report["non_btc_real_symbols_by_corpus"]
        synthetic = report["non_btc_synthetic_symbols_by_corpus"]
        assert "ETH_USDT" in report["non_btc_real_symbols"]
        assert "ETH_USDT" in report["non_btc_synthetic_symbols_present"]
        assert set(real) & set(synthetic) == set(), "a corpus cannot be both"
        assert "data/ohlcv" in synthetic
        assert "data/ohlcv" not in real
        assert any(p.startswith("data/real_multi") for p in real)

    def test_a_defect_named_file_does_not_become_an_instrument(self):
        """`ohlcv_high_low_not_bracketing.csv` once yielded `low_not`."""
        report = dc.build_report(repo_root=REPO)
        every = (report["non_btc_synthetic_symbols_present"]
                 + report["non_btc_real_symbols"])
        for name in every:
            assert "_" in name and name.isupper(), name

    @pytest.mark.parametrize("name,expected", [
        ("BITSTAMP_SPOT_BTC_USD_1D.csv.gz", "BTC_USD"),
        ("BYBIT_SPOT_ETH_USDT_1H.csv.gz", "ETH_USDT"),
        ("ohlcv_high_low_not_bracketing.csv", ""),
        ("ohlcv_bad_timestamp.csv", ""),
        ("l2_negative_latency.csv", ""),
        ("quotes_crossed_book.csv", ""),
        ("random.csv", ""),
    ])
    def test_the_symbol_parser_refuses_names_it_cannot_recognise(self, name, expected):
        assert dc._symbol_from_filename(name) == expected


class TestReportSchemaIsStable:

    REQUIRED_TOP = ("schema", "min_bars_for_stage1", "corpora",
                    "stage1_eligible_corpus_paths", "multi_asset_corpus_present",
                    "non_btc_real_symbols", "non_btc_real_symbols_by_corpus",
                    "non_btc_synthetic_symbols_present",
                    "non_btc_synthetic_symbols_by_corpus", "note")
    REQUIRED_RECORD = ("path", "exists", "interval_label", "is_synthetic",
                       "is_real_exchange_ohlcv", "symbols", "bar_count",
                       "timestamp_policy", "required_columns_ok",
                       "gap_policy_result", "min_bars_for_stage1",
                       "eligible_for_stage1", "ineligible_reasons")

    def test_the_top_level_keys_are_present(self):
        report = dc.build_report(repo_root=REPO)
        for key in self.REQUIRED_TOP:
            assert key in report, key

    def test_every_record_carries_every_field(self):
        for record in dc.build_report(repo_root=REPO)["corpora"]:
            for key in self.REQUIRED_RECORD:
                assert key in record, (record.get("path"), key)

    def test_the_schema_tag_is_pinned(self):
        assert dc.REPORT_SCHEMA == "data_intake/2"
        assert dc.build_report(repo_root=REPO)["schema"] == "data_intake/2"

    def test_it_round_trips_through_json(self):
        report = dc.build_report(repo_root=REPO)
        assert json.loads(json.dumps(report)) == report

    def test_the_note_states_eligibility_is_not_edge(self):
        note = dc.build_report(repo_root=REPO)["note"]
        assert "not edge" in note.lower()
        assert "CLOSED" in note


class TestThresholdsAreFrozen:

    def test_the_declared_numbers(self):
        assert dc.MIN_BARS_DAILY == 500
        assert dc.MIN_BARS_INTRADAY == 2000

    @pytest.mark.parametrize("seconds,expected", [
        (86400, 500), (604800, 500), (3600, 2000), (14400, 2000), (None, 2000),
    ])
    def test_the_threshold_applied_matches_the_resolution(self, seconds, expected):
        assert dc.min_bars_for(seconds) == expected

    def test_the_report_records_which_thresholds_were_used(self):
        thresholds = dc.build_report(repo_root=REPO)["min_bars_for_stage1"]
        assert thresholds == {"daily": 500, "intraday": 2000}


class TestEligibilityIsNotEdge:
    """The contract must not be able to clear a signal or reopen research."""

    def test_it_cannot_reach_cleared_edge_signal(self):
        """AST, not text — the module docstring names the field to disclaim it.

        This is the fourth slice in which a text-ban assertion had to become an
        AST one (see EDGE.md §12c). The rule is now firm: **a guard written as a
        text ban forces the code to stop explaining what it forbids.** What
        matters is whether any *code* references the field.
        """
        tree = ast.parse(inspect.getsource(dc))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                assert node.attr != "cleared_edge_signal"
            if isinstance(node, ast.Name):
                assert node.id != "cleared_edge_signal"

    def test_project_status_does_not_import_it(self):
        import project_status
        assert "data_contract" not in inspect.getsource(project_status)

    def test_the_snapshot_is_unchanged_by_this_slice(self):
        import config as _config
        import project_status as ps
        status = ps.current(_config.load({}))
        assert status.timing_skill_research == "CLOSED"
        assert status.cleared_edge_signal == \
            registration_invariant.stock_cleared_edge_signal()
        assert status.execution_mode == "paper"

    def test_an_eligible_corpus_does_not_make_a_signal_positive(self):
        """Eligible bars exist -- now including real ETH and SOL -- and STILL no
        artefact records a POSITIVE. Both true at once, which is the whole point:
        more data is not more edge."""
        import project_status as ps
        report = dc.build_report(repo_root=REPO)
        assert report["stage1_eligible_corpus_paths"]      # bars are available
        registration_invariant.assert_registration_is_sound(
            os.path.join(REPO, "artifacts"))       # nothing has cleared

    def test_research_close_is_not_weakened(self):
        with open(os.path.join(REPO, "RESEARCH_CLOSE_STAGE1.md"), encoding="utf-8") as h:
            text = h.read()
        assert "TIMING_SKILL_RESEARCH   CLOSED" in text
        assert "No deployable timing edge" in text or \
               "no deployable timing edge" in text.lower()


class TestItDoesNotTouchTheTradingPath:

    def test_it_imports_nothing_from_the_order_path(self):
        tree = ast.parse(inspect.getsource(dc))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        for banned in ("trading_engine", "bybit_connection", "risk_management",
                       "position_sizing", "ml_strategy", "policy"):
            assert banned not in imported, banned

    def test_it_names_no_gate_or_order_symbol(self):
        source = inspect.getsource(dc)
        for banned in ("place_order", "gate_order", "TradeIntent",
                       "kill_switch", "MAX_POSITION_SIZE_PCT"):
            assert banned not in source, banned

    def test_it_never_writes(self):
        """A scanner that can write is a scanner that can corrupt a corpus."""
        tree = ast.parse(inspect.getsource(dc))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "open":
                for arg in node.args[1:2]:
                    if isinstance(arg, ast.Constant):
                        assert "w" not in str(arg.value), arg.value
                for kw in node.keywords:
                    if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                        assert "w" not in str(kw.value.value)

    def test_it_does_not_start_a_measurement(self):
        """AST again: a comment cites `edge_measurement` to justify a threshold.

        Banning the word would delete the reasoning behind the 500-bar floor
        while leaving the module just as able to launch a run.
        """
        tree = ast.parse(inspect.getsource(dc))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        for banned in ("edge_measurement", "skill_test", "subprocess",
                       "run_evaluation", "multiprocessing"):
            assert banned not in imported, banned
        # And no dynamic escape hatch.
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "id", None) or getattr(
                    node.func, "attr", None)
                assert name not in {"system", "popen", "exec", "eval",
                                    "__import__", "run", "check_output"}, name


class TestTheOperatorTool:

    def test_it_exits_zero_and_writes_the_report(self, tmp_path, capsys):
        import data_intake_report as tool
        out = str(tmp_path / "report.json")
        assert tool.main(["--json-out", out]) == 0
        with open(out, encoding="utf-8") as handle:
            report = json.load(handle)
        assert report["schema"] == "data_intake/2"
        assert report["multi_asset_corpus_present"] is True

    def test_the_summary_names_the_synthetic_non_btc_symbols(self, capsys):
        import data_intake_report as tool
        tool.main([])
        out = capsys.readouterr().out
        assert "ETH_USDT" in out and "SOL_USDT" in out
        assert "SYNTHETIC" in out
        assert "Eligibility is NOT edge" in out

    def test_it_reports_reasons_for_every_ineligible_corpus(self, capsys):
        import data_intake_report as tool
        tool.main([])
        out = capsys.readouterr().out
        assert "INELIGIBLE" in out
        assert "data/ohlcv" in out

    def test_an_empty_repo_still_exits_zero(self, tmp_path, capsys):
        """No eligible corpus is a finding, not a tool failure."""
        import data_intake_report as tool
        assert tool.main(["--repo-root", str(tmp_path)]) == 0
        assert "NONE" in capsys.readouterr().out


class TestTheContractAgreesWithTheLoader:
    """Slice 38. A corpus Stage 1 can only partly read is not eligible.

    The defect this class exists for: the human's Bitstamp extension wrote
    `+00:00` where the older rows wrote `Z`, and left `trades_count` blank.
    `market_data.load_ohlcv` rejected all 571 new bars. `_inspect_ohlcv`
    counted CSV rows, so the report said 3,135 bars and `eligible: True` — and
    a measurement launched on the strength of that report would have run on
    the *old* 890-date window and been written up as the full history.

    Row count answers "what is in the file". Only the loader answers "what can
    Stage 1 read". The contract now requires both, and requires them to agree.
    """

    def _corpus_with_unreadable_tail(self, root, *, tail_suffix, tail_rows=100,
                                     total=600):
        directory = os.path.join(str(root), "mixed")
        ohlcv = os.path.join(directory, "ohlcv")
        os.makedirs(ohlcv, exist_ok=True)
        with open(os.path.join(directory, "MANIFEST.json"), "w") as handle:
            json.dump({"synthetic": False, "bar_seconds": 86400,
                       "source": {"exchange": "BITSTAMP"}}, handle)
        lines = [HEADER]
        for i in range(total):
            suffix = tail_suffix if i >= total - tail_rows else "Z"
            start = _stamp(i, 86400, suffix)
            end = _stamp(i + 1, 86400, suffix)
            lines.append(f"{start},{end},{start},{end},1,2,0.5,1.5,10,3")
        with gzip.open(os.path.join(ohlcv,
                                    "BITSTAMP_SPOT_BTC_USD_1D.csv.gz"),
                       "wt") as handle:
            handle.write("\n".join(lines) + "\n")
        return directory

    def test_a_corpus_whose_tail_the_loader_rejects_is_ineligible(self, tmp_path):
        """The decisive test. 600 rows, 100 of them unreadable -> refuse."""
        directory = self._corpus_with_unreadable_tail(tmp_path,
                                                      tail_suffix="+02:00")
        record = dc.scan_corpus(directory)
        assert record.bar_count == 600           # the file really has 600 rows
        assert record.loadable_bar_count == 500  # only 500 reach Stage 1
        assert record.rejected_rows == 100
        assert record.eligible_for_stage1 is False
        assert any("rejected by market_data.load_ohlcv" in r
                   for r in record.ineligible_reasons), record.ineligible_reasons

    def test_the_reason_names_both_numbers(self, tmp_path):
        """'Some rows were dropped' is not actionable; 100 of 600 is."""
        directory = self._corpus_with_unreadable_tail(tmp_path,
                                                      tail_suffix="+02:00")
        reason = " ".join(dc.scan_corpus(directory).ineligible_reasons)
        assert "100" in reason and "600" in reason and "500" in reason

    def test_the_utc_plus_zero_spelling_is_accepted_end_to_end(self, tmp_path):
        """`+00:00` IS UTC. Refusing it would fail closed on valid data.

        The fix had to distinguish 'a second spelling of the same instant'
        from 'a different instant'. This asserts the first half; the test
        above asserts the second.
        """
        directory = self._corpus_with_unreadable_tail(tmp_path,
                                                      tail_suffix="+00:00")
        record = dc.scan_corpus(directory)
        assert record.rejected_rows == 0
        assert record.loadable_bar_count == 600
        assert record.eligible_for_stage1 is True
        assert record.timestamp_policy == "UTC"

    def test_the_timestamp_policy_reads_every_row_not_the_first(self, tmp_path):
        """Sampling row 0 is how a mixed-spelling file called itself UTC."""
        directory = self._corpus_with_unreadable_tail(tmp_path,
                                                      tail_suffix="+02:00")
        assert dc.scan_corpus(directory).timestamp_policy == "unknown"

    def test_the_loader_is_the_one_the_measurement_uses(self):
        """No cheaper reimplementation: it would drift from what it checks."""
        source = inspect.getsource(dc._loadable_rows)
        tree = ast.parse(source)
        imported = {n.names[0].name for n in ast.walk(tree)
                    if isinstance(n, ast.Import)}
        assert "market_data" in imported
        assert any(isinstance(n, ast.Attribute) and n.attr == "load_ohlcv"
                   for n in ast.walk(tree))

    def test_every_eligible_real_corpus_loads_completely(self):
        """Applied to the repository as it actually stands."""
        for record in dc.scan_all(repo_root=REPO):
            if record.eligible_for_stage1:
                assert record.rejected_rows == 0, record.path
                assert record.loadable_bar_count == record.bar_count, record.path
