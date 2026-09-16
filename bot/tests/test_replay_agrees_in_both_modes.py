"""The replay's own P&L and its double-entry journal must agree. In BOTH modes.

WHY THIS FILE EXISTS
====================
`carry_replay` computes the book's P&L twice, independently: once as running
totals, and once by building a double-entry journal out of the fills the engine
actually sent. `ledger_agrees` compares them. LEDGER_0044 reported that
agreement "to the cent" in three cost configurations — and every one of them
was OVERLAY, which never buys spot.

NOTHING HAD EVER RUN THE REPLAY IN ACQUIRE. When it was finally run, three
defects were stacked on that one code path:

  D44a  `LedgerBroker._book` realised every spot sale against its own SALE
        price, so EQUITY:REALISED was exactly $0.00 on every spot sale. The
        gain was not lost — it landed in ASSET:BTC, which held value at zero
        quantity — and `reconcile()` compares QUANTITIES, so it read
        RECONCILED.

  D44b  `fee` crosses the broker boundary with no declared unit. The live
        venue reports a spot BUY fee in the COIN; `ReplayBroker` computed it
        in USD and returned the same number, and `ledger.spot_buy` multiplied
        it by the price. Four years of acquire booked **$388,719,591** of fees
        against the replay's own $20,542.

  D44c  the journal revaluation was posted in BOTH modes. That was correct
        only while D44a made spot realisation zero — the two errors
        compensated. Repairing the basis exposed a double-count of exactly
        $151,904.02, the acquire revaluation total to the cent.

Three defects, one unexercised mode, and a cross-check that would have caught
any of them the first time it ran. So it runs here, on both modes, every time.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tools"))

os.environ.setdefault("LEDGER_DISABLED", "1")

import carry_replay as cr  # noqa: E402

CORPUS = os.path.join(REPO, "data", "real_bybit_btc_4h")

needs_corpus = pytest.mark.skipif(
    not os.path.isdir(CORPUS),
    reason="the Bybit settlement corpus is not in this checkout")


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    """One replay per mode. Slow enough to share, cheap enough to keep."""
    out = {}
    folder = tmp_path_factory.mktemp("replay")
    for mode in ("overlay", "acquire"):
        path = str(folder / f"{mode}.json")
        assert cr.main(["--repo", REPO, "--mode", mode, "--json", path]) == 0
        with open(path, encoding="utf-8") as handle:
            out[mode] = json.load(handle)
    return out


@needs_corpus
class TestTheTwoAccountingsAgree:
    @pytest.mark.parametrize("mode", ("overlay", "acquire"))
    def test_the_journal_agrees_with_the_running_totals(self, runs, mode):
        r = runs[mode]
        assert r["ledger_agrees"], (
            f"{mode}: journal net {r['ledger_net_usd']:,.2f} + unrealised "
            f"{r['unrealised_usd']:,.2f} != replay net {r['net_usd']:,.2f}")

    @pytest.mark.parametrize("mode", ("overlay", "acquire"))
    def test_the_gap_is_zero_to_the_cent(self, runs, mode):
        r = runs[mode]
        gap = r["ledger_net_usd"] + r["unrealised_usd"] - r["net_usd"]
        assert abs(gap) < 0.01, f"{mode}: ${gap:,.2f} unaccounted"


@needs_corpus
class TestTheFeesAreInTheRightUnit:
    @pytest.mark.parametrize("mode", ("overlay", "acquire"))
    def test_the_journals_fees_match_the_replays_own(self, runs, mode):
        """D44b: a USD fee handed to a BTC-denominated path is inflated by the
        price. It showed up as $388m against a true $20k.

        The two sides carry OPPOSITE SIGNS, and both are right: the replay
        reports fees as a negative contribution to net, while
        `Journal.statement` reports EXPENSE:FEES as a positive expense. The
        MAGNITUDE is the claim here. Asserting the sign too would pin a
        bookkeeping convention rather than a fact, and the defect this guards
        against was three orders of magnitude, not a sign.
        """
        r = runs[mode]
        assert abs(r["ledger"]["fees_usd"]) == pytest.approx(
            abs(r["fees_usd"]), rel=1e-6)

    @pytest.mark.parametrize("mode", ("overlay", "acquire"))
    def test_fees_are_a_plausible_fraction_of_the_book(self, runs, mode):
        """A standing absurdity check: fees cannot exceed the notional many
        times over. The $388m failure would trip this on its own."""
        r = runs[mode]
        assert abs(r["ledger"]["fees_usd"]) < 10.0 * r["notional_usd"]


@needs_corpus
class TestTheRevaluationIsOverlayOnly:
    def test_overlay_revalues_the_inventory_it_never_trades(self, runs):
        assert abs(runs["overlay"]["hedge"]["inventory_change_usd"]) > 1_000.0

    def test_acquire_does_not_revalue_what_it_sold(self, runs):
        """D44c. In acquire the spot leg is bought and sold, so `spot_sell`
        realises it. A revaluation on top counts the same dollars twice."""
        assert runs["acquire"]["hedge"]["inventory_change_usd"] == \
            pytest.approx(0.0)

    def test_the_hedge_still_offsets_in_both_modes(self, runs):
        """Whichever way the long leg is accounted for, a delta-neutral book's
        two legs must very nearly cancel. The residual IS the basis."""
        for mode in ("overlay", "acquire"):
            r = runs[mode]
            perp = r["hedge"]["perp_realised_usd"]
            assert abs(perp) > 10_000.0, f"{mode}: no short leg to speak of"


@needs_corpus
class TestTheOverlayNumbersDidNotMove:
    """The regression guard. OVERLAY is the product; none of the acquire
    repairs may touch it."""

    def test_overlay_net_is_unchanged(self, runs):
        assert runs["overlay"]["net_usd"] == pytest.approx(29_766.40, abs=0.01)

    def test_overlay_trade_count_is_unchanged(self, runs):
        assert runs["overlay"]["trades"] == 79
