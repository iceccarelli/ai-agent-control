"""One asset over one regime was never evidence. These pin the second axis.

WHAT THIS IS FOR
================
Every carry number this programme has published — PHASE1_DECISION's matrix,
0041's walk-forward, D20's deflated Sharpe — came from BTC over 2022-08 →
2026-08, because `carry_backtest` hard-coded that triple in five places. ETH and
SOL had complete Binance funding + perp + spot corpora already committed to the
tree and no tool could read them, and Binance's REST had three more years of BTC
funding nobody had fetched (7,688 prints against the frozen 4,431).

0052 added two axes: `--asset {BTC,ETH,SOL}` and `--corpus {frozen,full}`.

THE POINT OF THESE TESTS IS THAT NOTHING MOVED
==============================================
Both axes default to what every previous caller got, so the numbers the slice
record pins are unchanged. `TestTheDefaultsAreExactlyWhatTheyWere` asserts that
structurally, and `test_frozen_btc_still_reproduces_phase1_decision` asserts it
numerically against the figure in the document.

AND THAT THE EXTENSION IS NOT TRUSTED
=====================================
`TestTheExtensionAgreesWithTheRecord` re-derives, on every run, the check that
justified using the extended corpus at all: on every timestamp the two overlap,
the funding rates are IDENTICAL. That is a property of the data, so it belongs
in the suite rather than in a paragraph someone wrote once.
"""
from __future__ import annotations

import csv
import gzip
import os
import sys

import pytest

# The data-read ledger exists to detect a corpus being looked at before a
# holdout is declared. A test run is not a look.
os.environ.setdefault("LEDGER_DISABLED", "1")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tools"))

import carry_backtest as cb          # noqa: E402
import corpus_health as ch           # noqa: E402

ASSETS = ("BTC", "ETH", "SOL")
CORPORA = ("frozen", "full")


def _has(corpus: str) -> bool:
    return all(os.path.exists(os.path.join(REPO, p))
               for p in ch.CORPORA[corpus]["BTC"])


needs_full = pytest.mark.skipif(
    not _has("full"),
    reason="the extended corpora (data/real_*_full) are not in this checkout")


class TestTheDefaultsAreExactlyWhatTheyWere:
    """Two new axes, and neither of them moved an existing caller."""

    def test_sources_defaults_to_the_frozen_btc_triple(self):
        assert cb._sources() is cb.ASSET_SOURCES["BTC"]
        assert cb._sources("BTC", "frozen") is cb.ASSET_SOURCES["BTC"]

    def test_spot_sources_is_still_the_btc_list(self):
        assert cb.SPOT_SOURCES is cb.ASSET_SOURCES["BTC"]["spot"]
        assert "BINANCE" in cb.SPOT_SOURCES[0][0]
        assert "BTC_USDT" in cb.SPOT_SOURCES[0][0]
        assert cb.SPOT_SOURCES[0][2] is True

    def test_corpus_health_SERIES_is_still_the_btc_frozen_triple(self):
        assert ch.SERIES == ch.series_for("BTC", "frozen")
        assert [name for name, _p, _c, _k in ch.SERIES] == \
            ["perp_1d", "spot_1d", "funding"]

    def test_the_defaults_are_named_not_implied(self):
        assert cb.DEFAULT_ASSET == "BTC"
        assert cb.DEFAULT_CORPUS == "frozen"
        assert ch.DEFAULT_ASSET == "BTC"
        assert ch.DEFAULT_CORPUS == "frozen"


class TestEveryCombinationResolvesToFilesThatExist:
    @pytest.mark.parametrize("asset", ASSETS)
    def test_the_frozen_triple_is_present(self, asset):
        for path in ch.CORPORA["frozen"][asset]:
            assert os.path.exists(os.path.join(REPO, path)), path

    @needs_full
    @pytest.mark.parametrize("asset", ASSETS)
    def test_the_full_triple_is_present(self, asset):
        for path in ch.CORPORA["full"][asset]:
            assert os.path.exists(os.path.join(REPO, path)), path

    @pytest.mark.parametrize("asset", ASSETS)
    def test_every_leg_is_the_same_venue_and_quote_currency(self, asset):
        """The basis is a DIFFERENCE. A spot leg from another venue or another
        quote currency books that venue's spread and that peg as carry."""
        for corpus in CORPORA:
            if corpus == "full" and not _has("full"):
                continue
            src = cb._sources(asset, corpus)
            legs = [src["perp"], src["funding"]] + [s[0] for s in src["spot"]]
            for leg in legs:
                assert "BINANCE" in leg, leg
                assert f"{asset}_USDT" in leg, leg


class TestTheExtensionAgreesWithTheRecord:
    """Verified on every run, not asserted once in a commit message."""

    @needs_full
    @pytest.mark.parametrize("asset", ASSETS)
    def test_the_funding_overlap_is_identical(self, asset):
        """Same timestamp, same RATE. Compared as numbers, deliberately.

        The first version of this test compared the raw CSV text and failed on
        48 BTC prints. Every one of them was the same number spelled two ways —
        the frozen corpus writes `0.00007497` and the extension's writer emits
        `7.497e-05` — with ZERO differences as floats. Comparing text asserts a
        formatting convention that was never a property of the data, and the
        invariant that justifies using the extension at all is that the RATE
        agrees. So this compares floats, and exactly: no tolerance, because
        these are the same values read twice, not a computation.
        """
        def load(path):
            with gzip.open(os.path.join(REPO, path), "rt") as handle:
                return {int(r["funding_time_ms"]): float(r["funding_rate"])
                        for r in csv.DictReader(handle)}

        frozen = load(ch.CORPORA["frozen"][asset][2])
        full = load(ch.CORPORA["full"][asset][2])
        common = set(frozen) & set(full)
        assert common, "the two corpora do not overlap at all"
        mismatched = [ms for ms in common if frozen[ms] != full[ms]]
        assert not mismatched, (
            f"{len(mismatched)} funding prints disagree IN VALUE between the "
            f"frozen corpus and the extension; the extension may not be used")

    @needs_full
    @pytest.mark.parametrize("asset", ASSETS)
    def test_the_extension_only_adds_history_at_the_front(self, asset):
        def stamps(path):
            with gzip.open(os.path.join(REPO, path), "rt") as handle:
                return [int(r["funding_time_ms"])
                        for r in csv.DictReader(handle)]

        frozen = stamps(ch.CORPORA["frozen"][asset][2])
        full = stamps(ch.CORPORA["full"][asset][2])
        assert len(full) > len(frozen)
        assert min(full) < min(frozen), "no earlier history was added"
        assert set(frozen) <= set(full), "the extension LOST a frozen print"


class TestTheNumbersCarryTheirLabel:
    def test_a_report_says_which_asset_and_which_corpus(self):
        r = cb.simulate(REPO, asset="ETH")
        assert r["asset"] == "ETH"
        assert r["corpus"] == "frozen"

    def test_frozen_btc_still_reproduces_phase1_decision(self):
        """The figure PHASE1_DECISION quotes: +9.66%/yr gated at 0% borrow,
        15 trades. If this moves, every document citing it is stale."""
        r = cb.simulate(REPO, borrow_apr=0.0, gated=True)
        assert r["net_annualised_pct"] == pytest.approx(9.66, abs=0.01)
        assert r["trades"] == 15

    def test_a_different_asset_is_a_different_number(self):
        btc = cb.simulate(REPO, borrow_apr=0.0, gated=True, asset="BTC")
        eth = cb.simulate(REPO, borrow_apr=0.0, gated=True, asset="ETH")
        assert btc["net_annualised_pct"] != eth["net_annualised_pct"]
        assert btc["spot_source"] != eth["spot_source"]

    @needs_full
    def test_the_full_corpus_is_a_longer_window(self):
        frozen = cb.simulate(REPO, asset="BTC", corpus="frozen")
        full = cb.simulate(REPO, asset="BTC", corpus="full")
        assert full["years"] > frozen["years"] + 2.5


class TestRefusals:
    """Refusing beats falling back to BTC: a number labelled with the wrong
    asset is worse than no number."""

    def test_an_unknown_asset_is_refused(self):
        with pytest.raises(SystemExit):
            cb._sources("DOGE")

    def test_an_unknown_corpus_is_refused(self):
        with pytest.raises(SystemExit):
            cb._sources("BTC", "nonsense")

    def test_corpus_health_refuses_both(self):
        with pytest.raises(ValueError):
            ch.series_for("DOGE")
        with pytest.raises(ValueError):
            ch.series_for("BTC", "nonsense")

    def test_the_settlement_clock_refuses_a_non_btc_asset(self):
        """The 8h corpora exist for BTC alone. Running BTC prices under an ETH
        label is the silent mislabelling this refuses."""
        with pytest.raises(SystemExit):
            cb.main(["--repo", REPO, "--clock", "8h", "--asset", "ETH"])

    def test_the_settlement_clock_refuses_the_extended_corpus(self):
        """There is no 4h extended corpus, so this combination would hand back
        the frozen settlement numbers under a 'full' label."""
        with pytest.raises(SystemExit):
            cb.main(["--repo", REPO, "--clock", "8h", "--corpus", "full"])
