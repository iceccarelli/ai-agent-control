"""Slice 55 — the funding and linear USDT-M corpora, and why they are eligible.

This is the first slice in the programme to admit a NEW asset class, and the
temptation it creates is specific: to loosen the OHLCV contract until a
funding-rate series passes an OHLCV test. That would weaken the check for every
corpus in the repository in order to accommodate one that is not OHLCV at all.

So two separate things happened, and both are tested here:

* `scan_corpus` learned a second MANIFEST *vocabulary* — `venue` + `source` +
  `interval` alongside `source.exchange` + `bar_seconds`. It learned nothing
  about what makes data trustworthy, and the synthetic check is untouched;
* funding got its own `scan_funding_corpus`, with criteria that suit a rate
  series: parseable, strictly increasing, no duplicate stamps, finite rates,
  and enough prints.

The tests below hold both to the standard the OHLCV contract already meets, and
the decisive one is `test_a_synthetic_funding_corpus_is_refused` — a corpus that
declares itself generated must fail no matter how clean its numbers are.
"""
from __future__ import annotations

import csv
import datetime as dt
import gzip
import json
import os
import sys
import tempfile

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tools"))

import data_contract as dc  # noqa: E402

FUNDING_DIR = "data/real_funding"
LINEAR_DIR = "data/real_linear_1d"
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")


def _manifest(directory):
    with open(os.path.join(REPO, directory, "MANIFEST.json"),
              encoding="utf-8") as handle:
        return json.load(handle)


# ---------------------------------------------------------------------------
# the manifests
# ---------------------------------------------------------------------------


class TestBothCorporaDeclareThemselvesReal:

    @pytest.mark.parametrize("directory", [FUNDING_DIR, LINEAR_DIR])
    def test_synthetic_is_false(self, directory):
        """The one field that stands alone. If it were true, nothing else matters."""
        assert _manifest(directory)["synthetic"] is False

    @pytest.mark.parametrize("directory", [FUNDING_DIR, LINEAR_DIR])
    def test_the_venue_and_endpoint_are_named(self, directory):
        manifest = _manifest(directory)
        assert manifest["venue"] == "binance_usdtm"
        assert manifest["source"]
        assert manifest["endpoint"].startswith("GET /fapi/")

    @pytest.mark.parametrize("directory", [FUNDING_DIR, LINEAR_DIR])
    def test_every_declared_symbol_is_present(self, directory):
        manifest = _manifest(directory)
        assert sorted(manifest["symbols"]) == sorted(SYMBOLS)
        assert sorted(manifest["date_range_utc"]) == sorted(SYMBOLS)


# ---------------------------------------------------------------------------
# the files themselves — read, do not trust
# ---------------------------------------------------------------------------


class TestTheFilesMatchWhatTheManifestClaims:
    """A manifest is a claim. These tests open the files and check it."""

    def _rows(self, directory, subdir, filename):
        path = os.path.join(REPO, directory, subdir, filename)
        assert os.path.exists(path), path
        with gzip.open(path, "rt", encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))

    @pytest.mark.parametrize("symbol", SYMBOLS)
    def test_funding_prefix_matches_the_pinned_eligibility_record(self, symbol):
        """The ELIGIBILITY ARTEFACT pins the measured prefix. Assert against it.

        Slice 62 asserted this against the MANIFEST, because the manifest was
        then stale at exactly the prefix length and the two agreed. That
        agreement was a property of one moment, not a relationship, and slice
        64 ended it: the human updated the manifests, so they now describe the
        FILE. EDGE.md §47c.

        The two records describe different things and each is now asserted
        against the thing it describes:

        * `artifacts/slice55_data_eligibility.json` — DATED. Pins the measured
          prefix. Its counts and digests never move. Checked here;
        * `data/*/MANIFEST.json` — LIVING. Describes the file on disk. Checked
          in `TestTheManifestsDescribeTheFilesBesideThem`.
        """
        path = (f"{FUNDING_DIR}/funding/"
                f"BINANCE_LINEAR_{symbol[:3]}_USDT_FUNDING.csv.gz")
        rows = self._rows(FUNDING_DIR, "funding",
                          f"BINANCE_LINEAR_{symbol[:3]}_USDT_FUNDING.csv.gz")

        import tools.corpus_prefix as cp  # noqa: PLC0415
        pinned = cp.pins()[path]
        result = cp.check(path)

        assert result.append_only, result.why_not()
        assert len(rows) >= pinned["rows"], "a corpus may grow; never shrink"

        prefix = rows[:pinned["rows"]]
        assert prefix[0]["funding_time"] == pinned["first_utc"]
        assert prefix[-1]["funding_time"] == pinned["last_utc"]
        assert result.prefix_sha256 == pinned["sha256_uncompressed"]

    @pytest.mark.parametrize("symbol", SYMBOLS)
    def test_funding_times_strictly_increase_with_no_duplicates(self, symbol):
        rows = self._rows(FUNDING_DIR, "funding",
                          f"BINANCE_LINEAR_{symbol[:3]}_USDT_FUNDING.csv.gz")
        stamps = [dt.datetime.fromisoformat(r["funding_time"]) for r in rows]
        assert len(set(stamps)) == len(stamps)
        assert all(b > a for a, b in zip(stamps, stamps[1:]))

    @pytest.mark.parametrize("symbol", SYMBOLS)
    def test_every_funding_rate_is_finite_and_plausible(self, symbol):
        import math
        rows = self._rows(FUNDING_DIR, "funding",
                          f"BINANCE_LINEAR_{symbol[:3]}_USDT_FUNDING.csv.gz")
        rates = [float(r["funding_rate"]) for r in rows]
        assert all(math.isfinite(x) for x in rates)
        # Binance caps funding well inside +/-5% per print; anything outside
        # that band would be a parsing error wearing a plausible costume.
        assert all(abs(x) <= 0.05 for x in rates)

    def test_the_funding_cadence_is_not_assumed_to_be_uniform(self):
        """SOLUSDT changed cadence inside the sample, and that is a venue fact.

        Its minimum gap is 2 hours where BTC's and ETH's are a flat 8. Nothing
        in the join policy assumes a fixed spacing — it takes the LAST print at
        or before the decision time, whatever the cadence — and this test
        records the irregularity so a future reader does not treat it as
        corruption.
        """
        import tools.corpus_prefix as cp  # noqa: PLC0415
        gaps = {}
        for symbol in SYMBOLS:
            path = (f"{FUNDING_DIR}/funding/"
                    f"BINANCE_LINEAR_{symbol[:3]}_USDT_FUNDING.csv.gz")
            # Measured over the PINNED PREFIX. The cadence of history is the
            # claim this test was written to make, and history is the prefix.
            hours = cp.funding_gap_hours(path, last=cp.pins()[path]["rows"])
            gaps[symbol] = (min(hours), max(hours))
        assert gaps["BTCUSDT"] == (8.0, 8.0)
        assert gaps["ETHUSDT"] == (8.0, 8.0)
        assert gaps["SOLUSDT"][0] < 8.0, gaps["SOLUSDT"]

    def test_the_only_missing_funding_print_is_the_known_seam(self):
        """A hole is named, never absorbed into a widened bound.

        Slice 62's extension began at the next DAY boundary rather than the
        next PRINT, so `2026-08-09T16:00Z` is absent. Widening the BTC bound
        above to `(8.0, 16.0)` would have made the suite green and made the
        hole invisible — a threshold moved to accommodate a defect, which is
        the thing this programme refuses to do.

        Instead the hole is enumerated. If a SECOND one ever appears, this
        fails, and it fails naming the timestamp. EDGE.md §45d.
        """
        import tools.corpus_prefix as cp  # noqa: PLC0415
        known = ["2026-08-09T16:00:00+00:00"]
        assert cp.missing_funding_prints(cp.FUNDING_BTC) == known
        # The hole is after t1, so no measured number depends on it.
        import market_data as md  # noqa: PLC0415
        assert all(md.parse_timestamp(stamp) > cp.t1_micros()
                   for stamp in known)

    def test_the_unextended_eth_series_has_no_holes_at_all(self):
        """A control for the test above: absent the extension, there is no gap.

        Without this, "exactly one missing print" could be an artefact of the
        checker rather than a fact about the file. ETHUSDT is the right
        control: a flat 8-hour cadence, and untouched by slice 62's extension.
        """
        import tools.corpus_prefix as cp  # noqa: PLC0415
        path = (f"{FUNDING_DIR}/funding/"
                "BINANCE_LINEAR_ETH_USDT_FUNDING.csv.gz")
        assert cp.check(path).appended_rows == 0
        assert cp.missing_funding_prints(path, cadence_hours=8.0) == []

    def test_the_sol_series_is_excluded_from_the_hole_check_and_why(self):
        """SOL is not a control, and pretending otherwise would be dishonest.

        `missing_funding_prints` asks "which timestamps would a series of THIS
        FIXED cadence contain". SOLUSDT changed cadence inside the sample — a
        venue fact the test above this one records — so no single cadence
        describes it and the question has no meaningful answer for it. Choosing
        whichever cadence made the assertion pass would be fitting the check to
        the data.

        What IS asserted for SOL is the thing that actually matters: its file
        was not touched by this slice's extension.
        """
        import tools.corpus_prefix as cp  # noqa: PLC0415
        path = (f"{FUNDING_DIR}/funding/"
                "BINANCE_LINEAR_SOL_USDT_FUNDING.csv.gz")
        hours = set(cp.funding_gap_hours(path))
        assert len(hours) > 1, "SOL's cadence was uniform after all"
        assert cp.check(path).appended_rows == 0

    @pytest.mark.parametrize("symbol", SYMBOLS)
    def test_linear_bars_match_and_are_sane(self, symbol):
        rows = self._rows(LINEAR_DIR, "ohlcv",
                          f"BINANCE_LINEAR_{symbol[:3]}_USDT_1D.csv.gz")
        path = f"{LINEAR_DIR}/ohlcv/BINANCE_LINEAR_{symbol[:3]}_USDT_1D.csv.gz"

        import tools.corpus_prefix as cp  # noqa: PLC0415
        pinned = cp.pins()[path]
        result = cp.check(path)
        assert result.pinned_rows == pinned["rows"] == 1461
        assert result.prefix_sha256 == pinned["sha256_uncompressed"]
        assert result.append_only, result.why_not()
        assert len(rows) >= 1461

        # Sanity is asserted over EVERY row, prefix and extension alike. An
        # appended bar gets no easier a test than a measured one.
        for row in rows:
            o = float(row["price_open"])
            h = float(row["price_high"])
            l = float(row["price_low"])
            c = float(row["price_close"])
            assert h >= max(o, c) and l <= min(o, c) and l > 0.0

    @pytest.mark.parametrize("symbol", SYMBOLS)
    def test_linear_bars_load_through_the_production_loader(self, symbol):
        """Eligibility means nothing if the loader cannot read the corpus.

        Slice 38's lesson: `data_contract` once certified a corpus whose rows
        the loader silently dropped.

        `verify=False` is deliberate and is the reason
        `tools/verify_slice55_corpora.py` exists. This MANIFEST carries no
        per-file checksum block, so `verify_manifest` refuses it — and
        weakening `verify_manifest` to accept a missing block would have
        removed a real integrity check from every corpus in the repository.
        The integrity claim for this corpus is carried instead by the pinned
        hashes in `artifacts/slice55_data_eligibility.json`, asserted below.
        """
        import backtest as bt
        import market_data as md
        loaded, _b, _n = md.load_corpus(os.path.join(REPO, LINEAR_DIR),
                                        verify=False)
        # The loader keys by `symbol_from_filename`, which joins base and
        # quote without a separator: BINANCE_LINEAR_BTC_USDT_1D -> BTCUSDT.
        assert symbol in loaded, sorted(loaded)
        bars = [bt.Bar(b.start_ms, b.open, b.high, b.low, b.close, b.volume)
                for b in loaded[symbol]]
        # >= rather than ==: a corpus may grow. The loader must read every row
        # that is there, and the count it reports must match the file rather
        # than a number frozen into this test. `corpus_prefix` is what pins
        # the measured portion.
        import tools.corpus_prefix as cp  # noqa: PLC0415
        path = f"{LINEAR_DIR}/ohlcv/BINANCE_LINEAR_{symbol[:3]}_USDT_1D.csv.gz"
        assert len(bars) >= 1461
        assert len(bars) == cp.check(path).rows_on_disk, (
            "the loader dropped rows the file contains — slice 38's defect")
        assert bars[0].start_ms < bars[-1].start_ms

    def test_the_strict_manifest_verifier_was_not_weakened(self):
        """It must still refuse this corpus, for the reason it always would.

        If this ever stops raising, someone has loosened `verify_manifest` to
        admit a checksum-less MANIFEST — which is exactly the shortcut this
        slice declined to take.
        """
        import market_data as md
        with pytest.raises(md.ManifestError) as excinfo:
            md.load_corpus(os.path.join(REPO, LINEAR_DIR))
        assert "lists no files" in str(excinfo.value)


# ---------------------------------------------------------------------------
# the contract
# ---------------------------------------------------------------------------


class TestTheContractAcceptsBothCorporaHonestly:

    def test_the_linear_corpus_is_stage1_eligible(self):
        record = dc.scan_corpus(LINEAR_DIR)
        assert record.eligible_for_stage1, record.ineligible_reasons
        assert record.is_synthetic is False
        assert record.is_real_exchange_ohlcv is True
        assert record.interval_label == "D"
        assert record.bar_count == record.loadable_bar_count == 1461
        assert record.rejected_rows == 0

    def test_the_funding_corpus_is_stage1_eligible(self):
        record = dc.scan_funding_corpus(FUNDING_DIR)
        assert record.eligible_for_stage1, record.ineligible_reasons
        assert record.is_synthetic is False
        assert record.exchange == "binance_usdtm"
        assert record.interval_seconds == 28800
        assert sorted(record.symbols) == sorted(SYMBOLS)
        assert record.duplicate_timestamps == 0
        assert record.non_monotonic == 0
        assert record.non_finite_rates == 0
        assert all(n >= dc.MIN_FUNDING_PRINTS
                   for n in record.rows_by_symbol.values())

    def test_funding_is_not_admitted_through_the_ohlcv_path(self):
        """The loosening that was NOT done, asserted so it cannot creep in.

        A funding corpus must remain ineligible as OHLCV. If this ever passes,
        someone has widened `scan_corpus` until a rate series looks like bars.
        """
        record = dc.scan_corpus(FUNDING_DIR)
        assert record.eligible_for_stage1 is False
        assert any("not an OHLCV corpus" in why
                   for why in record.ineligible_reasons)


class TestTheSyntheticCheckStillStandsAlone:
    """The one property no vocabulary change may weaken."""

    def _corpus(self, directory, manifest, rows):
        os.makedirs(os.path.join(directory, "funding"), exist_ok=True)
        with open(os.path.join(directory, "MANIFEST.json"), "w",
                  encoding="utf-8") as handle:
            json.dump(manifest, handle)
        path = os.path.join(directory, "funding", "X.csv.gz")
        with gzip.open(path, "wt", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["funding_time", "symbol", "funding_rate"])
            writer.writerows(rows)

    def _clean_rows(self, n=1200):
        start = dt.datetime(2023, 1, 1, tzinfo=dt.timezone.utc)
        return [[(start + dt.timedelta(hours=8 * i)).isoformat(),
                 "XUSDT", "0.0001"] for i in range(n)]

    def test_a_synthetic_funding_corpus_is_refused(self):
        """Decisive. Perfect data, declared generated, still refused."""
        with tempfile.TemporaryDirectory() as directory:
            self._corpus(directory,
                         {"synthetic": True, "venue": "binance_usdtm",
                          "source": "public_rest_funding_history",
                          "interval": "8h"},
                         self._clean_rows())
            record = dc.scan_funding_corpus(directory)
            assert record.eligible_for_stage1 is False
            assert any("SYNTHETIC" in why for why in record.ineligible_reasons)

    def test_a_bare_venue_string_confers_no_provenance(self):
        """`venue` alone is a label. Provenance needs the fetch named too."""
        with tempfile.TemporaryDirectory() as directory:
            self._corpus(directory,
                         {"synthetic": False, "venue": "binance_usdtm",
                          "interval": "8h"},
                         self._clean_rows())
            record = dc.scan_funding_corpus(directory)
            assert record.eligible_for_stage1 is False
            assert any("provenance not established" in why
                       for why in record.ineligible_reasons)

    def test_duplicate_and_out_of_order_stamps_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            rows = self._clean_rows()
            rows.append(list(rows[-1]))                     # duplicate
            self._corpus(directory,
                         {"synthetic": False, "venue": "binance_usdtm",
                          "source": "public_rest_funding_history",
                          "interval": "8h"}, rows)
            record = dc.scan_funding_corpus(directory)
            assert record.eligible_for_stage1 is False
            assert record.duplicate_timestamps >= 1
            assert record.non_monotonic >= 1

    def test_a_non_finite_rate_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            rows = self._clean_rows()
            rows[10][2] = "nan"
            self._corpus(directory,
                         {"synthetic": False, "venue": "binance_usdtm",
                          "source": "public_rest_funding_history",
                          "interval": "8h"}, rows)
            record = dc.scan_funding_corpus(directory)
            assert record.eligible_for_stage1 is False
            assert record.non_finite_rates >= 1

    def test_too_few_prints_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            self._corpus(directory,
                         {"synthetic": False, "venue": "binance_usdtm",
                          "source": "public_rest_funding_history",
                          "interval": "8h"},
                         self._clean_rows(n=50))
            record = dc.scan_funding_corpus(directory)
            assert record.eligible_for_stage1 is False
            assert any("funding prints" in why
                       for why in record.ineligible_reasons)


class TestTheVocabularyChangeDidNotWeakenTheOhlcvContract:
    """The corpora that were refused before must still be refused."""

    @pytest.mark.parametrize("directory", ["data", "data/ohlcv",
                                           "data/orderbook", "data/quotes"])
    def test_synthetic_corpora_are_still_refused(self, directory):
        record = dc.scan_corpus(directory)
        assert record.eligible_for_stage1 is False
        assert record.is_synthetic is True

    @pytest.mark.parametrize("directory", ["data/real_1d", "data/real_multi_1d"])
    def test_the_previously_eligible_corpora_are_unchanged(self, directory):
        record = dc.scan_corpus(directory)
        assert record.eligible_for_stage1 is True
        assert record.is_synthetic is False

    def test_an_unknown_interval_label_stays_unknown(self):
        """A closed vocabulary, not a parser."""
        assert dc.manifest_bar_seconds({"interval": "fortnightly"}) is None
        assert dc.manifest_bar_seconds({"interval": "1d"}) == 86400
        assert dc.manifest_bar_seconds({"bar_seconds": 86400}) == 86400
        assert dc.manifest_bar_seconds({}) is None
        assert dc.manifest_bar_seconds(None) is None


class TestTheEligibilityArtefactPinsWhatIsOnDisk:
    """The hashes are the integrity claim, so they are asserted, not filed.

    They were computed by this slice from the files as received. That detects
    drift; it does not attest the fetch, and the artefact says so in
    `checksum_provenance` rather than claiming a stronger verification than
    the repository can honestly perform.
    """

    def _payload(self):
        path = os.path.join(REPO, "artifacts", "slice55_data_eligibility.json")
        assert os.path.exists(path), "run tools/verify_slice55_corpora.py"
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)

    def test_it_records_both_corpora_as_eligible(self):
        payload = self._payload()
        assert payload["schema"] == "data_eligibility/1"
        assert payload["all_checks_passed"] is True
        for directory in (FUNDING_DIR, LINEAR_DIR):
            block = payload["corpora"][directory]
            assert block["manifest_synthetic"] is False
            assert block["manifest_venue"] == "binance_usdtm"
            assert block["contract_eligible"] is True
            assert block["contract_reasons"] == []

    def test_the_recorded_hashes_match_the_measured_prefix_today(self):
        """Decisive: recompute, do not trust the record.

        The recorded digest is the digest of the corpus AS OF THE PIN, so it is
        recomputed over the prefix at the recorded row count — and the file is
        additionally required to be an append of post-`t1` rows. Any edit to a
        recorded byte still fails. EDGE.md §45c.
        """
        import tools.corpus_prefix as cp  # noqa: PLC0415
        payload = self._payload()
        for directory, block in payload["corpora"].items():
            for symbol, info in block["symbols"].items():
                path = os.path.join(REPO, info["file"])
                assert os.path.exists(path), path
                result = cp.check(info["file"])
                assert result.prefix_sha256 == info["sha256_uncompressed"], (
                    directory, symbol, "MEASURED HISTORY REWRITTEN")
                assert result.append_only, result.why_not()

    def test_the_recorded_row_counts_still_describe_the_measured_prefix(self):
        """Written in slice 55 as `... match the manifests`, and that was right
        while the manifests were a description of the measured corpus.

        They are not any more. Slice 64's pack updated them to describe the
        FILES, so the eligibility artefact and the manifest now legitimately
        disagree — and asserting their agreement would force one of the two to
        be wrong forever. EDGE.md §47c.

        What survives is the claim that matters: the artefact's counts are
        still the counts of the corpus PREFIX it measured, recomputed from
        disk rather than trusted.
        """
        import tools.corpus_prefix as cp  # noqa: PLC0415
        payload = self._payload()
        checked = 0
        for directory in (FUNDING_DIR, LINEAR_DIR):
            for symbol, info in payload["corpora"][directory]["symbols"].items():
                result = cp.check(info["file"])
                assert result.pinned_rows == info["rows"], (directory, symbol)
                assert result.prefix_sha256 == info["sha256_uncompressed"], (
                    directory, symbol)
                assert result.rows_on_disk >= info["rows"], (directory, symbol)
                checked += 1
        assert checked == 6, checked

    def test_it_does_not_overclaim_the_verification(self):
        """The sentence that keeps this honest must stay in the artefact."""
        note = self._payload()["checksum_provenance"]
        assert "computed BY THIS SLICE" in note
        assert "do NOT independently attest the fetch" in note

    def test_it_records_that_the_loader_check_was_not_weakened(self):
        block = self._payload()["corpora"][LINEAR_DIR]
        assert block["loader_manifest_has_checksums"] is False
        assert "was NOT weakened" in block["loader_note"]


# ---------------------------------------------------------------------------
# the manifests, checked against the files beside them.  EDGE.md §47b
# ---------------------------------------------------------------------------


class TestTheManifestsDescribeTheFilesBesideThem:
    """A manifest is a claim about a corpus. Slice 64's is false for four files.

    The human updated both MANIFESTs this slice — the first time since the
    extensions began — by BROADCASTING BTCUSDT's new row counts and end dates
    across all three symbols. ETH's and SOL's four files were not extended at
    all; they are byte-identical to slice 63's.

    One entry is worse than merely wrong. SOL funding holds 4458 prints and the
    PREVIOUS manifest said 4458, correctly. The update replaced a right number
    with 4392, so the record now UNDERSTATES a corpus by 66 rows — an
    overstatement sends a reader looking for data that is not there, an
    understatement lets them conclude data is missing when it is present, and
    this one edit did both.

    THE MANIFEST IS NOT EDITED BY THIS PROGRAMME. Rewriting a data-provenance
    record to state whatever the files say would be asserting a provenance it
    cannot attest: nothing here knows independently that ETH should hold 1461
    rows rather than having lost two. A manifest that agrees with disk by
    construction certifies nothing.

    So the false claims are ENUMERATED BY NAME, exactly as the funding seam was
    in §45d. This passes on the delivered tree and goes red the moment any of
    them changes — including when a human FIXES them, which is the signal that
    the enumeration can be retired.
    """

    # Re-pinned in slice 65. The enumeration went red, which is what it was
    # built to do — but the signal read RE-BROADCAST, not repaired: ETH's and
    # SOL's four files are byte-identical to slice 64's, and their DECLARED
    # counts moved anyway, tracking BTC's. EDGE.md §48a.
    #
    # Re-pinned again in slice 66 — THIRD occurrence. ETH's and SOL's files
    # are byte-identical to slice 65's and their declared counts moved anyway.
    #
    #                       s64 decl   s65 decl   s66 decl   disk
    #   ETH_USDT_1D             1463       1464       1465   1461
    #   SOL_USDT_1D             1463       1464       1465   1461
    #   ETH_USDT_FUNDING        4392       4394       4397   4383
    #   SOL_USDT_FUNDING        4392       4394       4397   4458
    KNOWN_FALSE = {
        ("data/real_linear_1d", "ETHUSDT"): (1465, 1461),
        ("data/real_linear_1d", "SOLUSDT"): (1465, 1461),
        ("data/real_funding", "ETHUSDT"): (4397, 4383),
        ("data/real_funding", "SOLUSDT"): (4397, 4458),
    }
    # Slice 67: these are the values slice 66 declared AND the values still
    # declared today — the broadcast stopped, so "previous" and "current" now
    # coincide for ETH and SOL. That coincidence is the finding.
    PREVIOUSLY_DECLARED = {
        ("data/real_linear_1d", "ETHUSDT"): 1465,
        ("data/real_linear_1d", "SOLUSDT"): 1465,
        ("data/real_funding", "ETHUSDT"): 4397,
        ("data/real_funding", "SOLUSDT"): 4397,
    }
    # Every value SOL funding has been declared while the file held 4458.
    SOL_FUNDING_DECLARED_HISTORY = (4392, 4394, 4397)

    @staticmethod
    def _on_disk(directory, symbol):
        import tools.corpus_prefix as cp  # noqa: PLC0415
        subdir, suffix = (("ohlcv", "1D") if "linear" in directory
                          else ("funding", "FUNDING"))
        path = (f"{directory}/{subdir}/"
                f"BINANCE_LINEAR_{symbol[:3]}_USDT_{suffix}.csv.gz")
        return path, len(cp.read_rows(path))

    def test_the_measured_products_entries_are_accurate(self):
        """BTCUSDT is the only symbol this programme measures, and its two
        manifest entries agree with the files. That is why the defect below
        taints no claim in slice 64."""
        for directory in (LINEAR_DIR, FUNDING_DIR):
            declared = _manifest(directory)["date_range_utc"]["BTCUSDT"]
            _path, rows = self._on_disk(directory, "BTCUSDT")
            assert rows == declared["rows"], (directory, rows, declared)

    @pytest.mark.parametrize("directory,symbol", sorted(KNOWN_FALSE))
    def test_each_known_false_claim_is_still_exactly_as_recorded(
            self, directory, symbol):
        """Named, not absorbed. If a count moves — fixed or worsened — this
        fails and says which."""
        declared_rows, disk_rows = self.KNOWN_FALSE[(directory, symbol)]
        actual_declared = _manifest(directory)["date_range_utc"][symbol]["rows"]
        _path, actual_disk = self._on_disk(directory, symbol)
        assert actual_declared == declared_rows, (
            f"{directory}/{symbol}: the manifest now claims {actual_declared}, "
            f"not the {declared_rows} recorded in EDGE.md §47b. If a human has "
            f"repaired the manifest, retire this enumeration.")
        assert actual_disk == disk_rows, (
            f"{directory}/{symbol}: the file now holds {actual_disk} rows, not "
            f"{disk_rows}. The corpus moved; re-examine §47b.")
        assert actual_declared != actual_disk

    def test_the_defect_set_is_exactly_four_files(self):
        """A sweep, so a FIFTH false claim cannot hide behind the four named."""
        disagreeing = set()
        for directory in (LINEAR_DIR, FUNDING_DIR):
            for symbol in SYMBOLS:
                declared = _manifest(directory)["date_range_utc"][symbol]
                _path, rows = self._on_disk(directory, symbol)
                if rows != declared["rows"]:
                    disagreeing.add((directory, symbol))
        assert disagreeing == set(self.KNOWN_FALSE), disagreeing

    def test_the_sol_funding_entry_is_wrong_in_a_third_different_way(self):
        """The sharpest of the four, stated on its own so it is not skimmed.

        SOL funding holds 4458 prints and has never changed. Its declared count
        was correct until slice 64 (4458), then 4392, then 4394, and is now
        4397 — wrong in THREE successive and DIFFERENT ways while the file
        underneath it sat still. A number that moves every slice under a corpus
        that never moves is not a description of anything. EDGE.md §48a, §49d.
        """
        declared = _manifest(FUNDING_DIR)["date_range_utc"]["SOLUSDT"]["rows"]
        _path, disk = self._on_disk(FUNDING_DIR, "SOLUSDT")
        assert disk == 4458, "SOL funding changed; re-examine §49d"
        assert declared == self.SOL_FUNDING_DECLARED_HISTORY[-1] == 4397
        assert declared not in self.SOL_FUNDING_DECLARED_HISTORY[:-1]
        assert len(set(self.SOL_FUNDING_DECLARED_HISTORY)) == 3
        assert all(value != disk
                   for value in self.SOL_FUNDING_DECLARED_HISTORY)
        assert declared < disk, "the manifest understates the corpus"

    def test_the_broadcast_has_stopped(self):
        """RETIRED AND REPLACED in slice 67 — the guard fired on a REPAIR.

        As written (slice 65) this asserted that all three symbols declare the
        SAME count, the signature of writing BTC's figures into every entry,
        and its docstring said that if that ever stopped being true "the
        broadcast may have been repaired; re-examine EDGE.md §48a."

        It stopped being true. BTC advanced to 1466 / 4399 and ETH and SOL did
        not follow, for the first time in four slices. **A guard reporting good
        news** — it was written to fire on any change to the defect's shape, in
        either direction, and to tell the next reader which direction to look.

        What replaces it asserts the two facts that are now true and were not
        before: the broadcast is over, and the residue it left is still there.
        EDGE.md §50b.
        """
        for directory in (LINEAR_DIR, FUNDING_DIR):
            declared = {symbol: _manifest(directory)["date_range_utc"][symbol][
                "rows"] for symbol in SYMBOLS}
            assert declared["ETHUSDT"] == declared["SOLUSDT"], declared
            assert declared["BTCUSDT"] != declared["ETHUSDT"], (
                f"{directory}: BTC and ETH declare the same count again — the "
                f"broadcast may have resumed. Re-examine EDGE.md §50b. "
                f"{declared}")
            _path, btc_disk = self._on_disk(directory, "BTCUSDT")
            assert declared["BTCUSDT"] == btc_disk, (
                "the measured product's own entry is now wrong; every other "
                "reading in this class assumes it is right")

    def test_the_residue_is_frozen_at_slice66s_broadcast_values(self):
        """The damage the broadcast did, still undone.

        ETH and SOL carry 1465 and 4397 — slice 66's broadcast values, wrong
        when written and wrong now. Their true counts are 1461, 1461, 4383 and
        4458, and their files have never been touched. They were MOVING-wrong;
        they are now FROZEN-wrong. When a human finally corrects these four
        numbers this test fires, and that will again be good news.
        """
        import tools.corpus_prefix as cp  # noqa: PLC0415
        residue = {
            (LINEAR_DIR, "ETHUSDT"): (1465, 1461),
            (LINEAR_DIR, "SOLUSDT"): (1465, 1461),
            (FUNDING_DIR, "ETHUSDT"): (4397, 4383),
            (FUNDING_DIR, "SOLUSDT"): (4397, 4458),
        }
        for (directory, symbol), (declared, disk) in residue.items():
            actual = _manifest(directory)["date_range_utc"][symbol]["rows"]
            path, on_disk = self._on_disk(directory, symbol)
            assert actual == declared, (
                f"{directory}/{symbol}: declared {actual}, not the {declared} "
                f"slice 66 left behind. If a human has repaired the manifest, "
                f"retire this test. EDGE.md §50b.")
            assert on_disk == disk, (directory, symbol, on_disk)
            assert cp.check(path).appended_rows == 0, (
                f"{path} was extended after all; §50b needs revisiting")

    def test_the_declared_counts_no_longer_track_btc(self):
        """Written in slice 65 as `... track BTC rather than their own files`,
        which was the finding then. Slice 67 inverts it, because the behaviour
        inverted: ETH's and SOL's declarations stood still while BTC's moved.

        What is asserted now is the pair that distinguishes a stopped broadcast
        from a repaired manifest: the declarations did NOT move, and the files
        they describe did not move either — so they are neither tracking BTC
        nor describing themselves. EDGE.md §50b.
        """
        import tools.corpus_prefix as cp  # noqa: PLC0415
        for (directory, symbol), previous in self.PREVIOUSLY_DECLARED.items():
            now = _manifest(directory)["date_range_utc"][symbol]["rows"]
            path, _rows = self._on_disk(directory, symbol)
            assert now == previous, (
                f"{directory}/{symbol}: declaration moved from {previous} to "
                f"{now}; the broadcast may have resumed. EDGE.md §50b.")
            assert cp.check(path).appended_rows == 0, path
