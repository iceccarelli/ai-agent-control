"""The settlement clock reaches inception, and carries more than BTC.

WHY
===
D20 — "neither rule survives its own search width" — is this programme's
strongest negative finding, and it was computed on ONE asset over FOUR years:
the committed Bybit 4h corpus, 4,600 prints from 2022-08. `carry_walkforward`
and `carry_sweep` both called `load_bybit_settlements` unconditionally, so
there was no way to ask the question of any other asset or any other regime.

0052 widened the DAILY backtester to three assets and seven years. It did not
touch the settlement clock, and the 0052 commit message overstated that by
implying the walk-forward had been widened too. It had not.

The blocker turned out to be a hardcoded literal: `fetch_settlement_klines`
started a FRESH file at 2022-08-10 — the date the frozen daily corpus begins —
while Binance serves 8h klines from each contract's inception. That is now a
`--since` parameter, defaulting to the old literal so no existing caller moves.

WHAT THESE PIN
==============
The corpus properties the D20 re-run depends on. If a refetch ever changes
them, the numbers computed on top change with them, and that should fail here
rather than be discovered in a report.

The SOL caveat is pinned as a PROPERTY, not tolerated as noise: the loader
rounds a funding stamp to the 8h grid and silently drops anything more than
60s off it. That is lossless for BTC and ETH and drops exactly 75 SOL prints,
every one in the FTX week of 2022-11 when Binance shortened SOL's funding
interval (INVENTORY D46). They are real settlements the book would have been
paid at, so a SOL number from this path understates that week.
"""
from __future__ import annotations

import csv
import gzip
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tools"))

os.environ.setdefault("LEDGER_DISABLED", "1")

import carry_backtest as cb          # noqa: E402
import carry_walkforward as cw       # noqa: E402
import fetch_settlement_klines as fsk  # noqa: E402

EIGHT_H = os.path.join(REPO, "data", "real_settlement_8h")
SET_MS = 8 * 3600 * 1000

needs_8h = pytest.mark.skipif(
    not os.path.isdir(EIGHT_H),
    reason="the 8h settlement corpus is not in this checkout")

#: asset -> (funding prints, paired settlements, dropped)
EXPECTED = {"BTC": (7688, 7688, 0), "ETH": (7454, 7454, 0),
            "SOL": (6655, 6578, 77)}


def _paired(asset: str):
    """Replicate the loader's pairing rule, without invoking the loader."""
    def load8h(name):
        with gzip.open(os.path.join(EIGHT_H, name), "rt") as fh:
            return {int(r["open_time_ms"]) + SET_MS
                    for r in csv.DictReader(fh)}

    perp = load8h(f"BINANCE_PERP_{asset}USDT_8H.csv.gz")
    spot = load8h(f"BINANCE_SPOT_{asset}USDT_8H.csv.gz")
    path = os.path.join(REPO, cb.ASSET_SOURCES_FULL[asset]["funding"])
    with gzip.open(path, "rt") as fh:
        stamps = [int(r["funding_time_ms"]) for r in csv.DictReader(fh)]
    off_grid, missing, ok = [], [], []
    for ms in stamps:
        grid = int(round(ms / SET_MS)) * SET_MS
        if abs(grid - ms) > 60_000:
            off_grid.append(ms)
        elif grid not in perp or grid not in spot:
            missing.append(grid)
        else:
            ok.append(grid)
    return stamps, ok, off_grid, missing


@needs_8h
class TestNoCorpusWithoutProvenance:
    """The rule `fetch_binance_klines` states and this fetcher did not follow.

    It wrote SIX real Binance files and no MANIFEST.json, for eight slices.
    Nothing caught it because `data/real_settlement_8h` had never existed in a
    checkout; the moment it did, `test_every_corpus_file_is_listed` failed —
    the files had fallen through to the SYNTHETIC root manifest, which is the
    "a real file checked against a synthetic hash" case that test warns about.

    Fixing only the symptom (hand-writing the manifest) would leave the fetcher
    able to produce an unverifiable corpus the next time it runs somewhere new.
    """

    def test_the_fetcher_writes_a_manifest(self):
        assert hasattr(fsk, "write_manifest")

    def test_the_corpus_verifies_against_its_own_provenance(self):
        import market_data as md
        report = md.verify_manifest(EIGHT_H, deep=True)
        assert report.ok, report.problems
        assert report.checked == 6

    def test_it_declares_itself_real_not_synthetic(self):
        """`ManifestReport.synthetic` defaults to True fail-safe, so this is a
        claim the corpus has to make for itself."""
        import market_data as md
        assert md.verify_manifest(EIGHT_H).synthetic is False

    def test_the_manifest_names_the_endpoints_it_fetched_from(self):
        import json
        with open(os.path.join(EIGHT_H, "MANIFEST.json"), encoding="utf-8") as fh:
            manifest = json.load(fh)
        assert manifest["endpoints"]["perp"] == fsk.PERP_ENDPOINT
        assert manifest["endpoints"]["spot"] == fsk.SPOT_ENDPOINT


class TestTheFetcherStartIsAParameterNotALiteral:
    def test_the_default_is_unchanged(self):
        """Every caller that predates 0056 must write what it wrote before."""
        assert fsk.DEFAULT_SINCE == "2022-08-10"

    def test_run_accepts_a_since(self):
        import inspect
        assert "since" in inspect.signature(fsk.run).parameters

    def test_the_cli_exposes_it(self):
        import argparse
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with pytest.raises(SystemExit):
                fsk.main(["--help"])
        assert "--since" in buf.getvalue()


@needs_8h
class TestTheCorpusReachesInception:
    @pytest.mark.parametrize("asset,first", [("BTC", "2019"), ("ETH", "2019"),
                                             ("SOL", "2020")])
    def test_the_perp_leg_starts_at_the_contracts_inception(self, asset, first):
        path = os.path.join(EIGHT_H, f"BINANCE_PERP_{asset}USDT_8H.csv.gz")
        with gzip.open(path, "rt") as fh:
            row = next(csv.DictReader(fh))
        assert row["open_utc"].startswith(first), row["open_utc"]

    @pytest.mark.parametrize("asset", sorted(EXPECTED))
    def test_it_is_longer_than_the_bybit_corpus_it_replaces(self, asset):
        """The Bybit 4h corpus is 4,600 prints from 2022-06. The point of this
        work is regime coverage, so a shorter series would be pointless."""
        _stamps, ok, _off, _miss = _paired(asset)
        assert len(ok) > 4_600


@needs_8h
class TestThePairingIsLosslessWhereItClaimsToBe:
    @pytest.mark.parametrize("asset", sorted(EXPECTED))
    def test_the_counts_are_what_the_measurement_assumed(self, asset):
        prints, paired, dropped = EXPECTED[asset]
        stamps, ok, off, miss = _paired(asset)
        assert len(stamps) == prints
        assert len(ok) == paired
        assert len(off) + len(miss) == dropped

    @pytest.mark.parametrize("asset", ("BTC", "ETH"))
    def test_btc_and_eth_drop_nothing_at_all(self, asset):
        _stamps, _ok, off, miss = _paired(asset)
        assert not off and not miss

    @pytest.mark.parametrize("asset", sorted(EXPECTED))
    def test_there_are_no_interior_holes(self, asset):
        """A missing bar in the MIDDLE is a defect. SOL's two are at the
        contract's own inception edge, which is not one."""
        _stamps, _ok, _off, miss = _paired(asset)
        assert len(miss) <= 2


@needs_8h
class TestTheSolCaveatIsAPropertyNotNoise:
    """INVENTORY D46. Pinned so a SOL number can never quietly look clean."""

    def test_sols_dropped_prints_are_off_grid_not_missing(self):
        _stamps, _ok, off, miss = _paired("SOL")
        assert len(off) == 75
        assert len(miss) == 2

    def test_every_off_grid_print_is_in_the_ftx_week(self):
        import datetime as dt
        _stamps, _ok, off, _miss = _paired("SOL")
        days = {dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).date()
                for ms in off}
        assert min(days) >= dt.date(2022, 11, 9)
        assert max(days) <= dt.date(2022, 11, 18)

    def test_the_loader_says_so_in_its_docstring(self):
        """A caveat that lives only in a commit message enforces nothing."""
        assert "D46" in (cb.load_binance_settlements.__doc__ or "")


@needs_8h
class TestTheLoaderTakesAnAssetAndACorpus:
    def test_it_defaults_to_btc_frozen(self):
        import inspect
        params = inspect.signature(cb.load_binance_settlements).parameters
        assert params["asset"].default == cb.DEFAULT_ASSET
        assert params["corpus"].default == cb.DEFAULT_CORPUS

    @pytest.mark.parametrize("asset", sorted(EXPECTED))
    def test_each_asset_loads_and_names_itself(self, asset):
        rows, meta = cb.load_binance_settlements(REPO, asset, "full")
        assert rows
        assert meta["asset"] == asset
        assert meta["funding_dataset"] == f"BINANCE_LINEAR_{asset}_USDT_FUNDING"

    def test_a_different_asset_is_a_different_series(self):
        btc, _ = cb.load_binance_settlements(REPO, "BTC", "full")
        eth, _ = cb.load_binance_settlements(REPO, "ETH", "full")
        assert btc[0].perp != eth[0].perp


class TestTheWalkForwardRefusesAnImpossibleCombination:
    def test_bybit_plus_a_non_btc_asset_is_refused(self, capsys):
        """The committed Bybit corpus is BTC alone. Silently returning BTC
        numbers under an ETH label is the mislabelling this refuses."""
        assert cw.main(["--repo", REPO, "--venue", "bybit",
                        "--asset", "ETH"]) == 2
        assert "BTC only" in capsys.readouterr().err

    def test_its_dead_dataset_constant_is_marked_dead(self):
        """It occurs once, is read by nothing, and looks like it governs a
        certification. `carry_sweep` is the tool that certifies."""
        with open(os.path.join(REPO, "tools", "carry_walkforward.py"),
                  encoding="utf-8") as fh:
            body = fh.read()
        assert body.count("DATASET") == 1
        assert "unused" in body[body.index("DATASET") - 400:
                                body.index("DATASET") + 120]
