"""Every supported mode x style must reconcile. All of them, every run.

WHY THIS FILE EXISTS
====================
Four defects in a row were the same defect: a supported combination nobody
exercised.

    D44a  acquire realised every spot sale against its own sale price
    D44b  acquire inflated every spot buy fee by the BTC price ($388,719,591)
    D44c  acquire double-counted the spot leg via the journal revaluation
    D45   maker_first never booked the fill a cancel caught

Overlay was tested and acquire was not. Taker was tested and maker_first was
not. Every one of them was found by RUNNING the combination, not by reading
the code — and `carry_replay` has always computed the book's P&L twice,
independently, and compared them. That cross-check would have caught all four
on its first run against the path in question. It was only ever run on one.

So it runs on all of them here. This is a guard, not a hunt: at the time of
writing every combination already reconciles. The value is that it keeps doing
so.

`tests/test_carry_maker.py` already proves maker_first REACHES the engine.
This asks the different question: when it does, do the books still add up?
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
COMBINATIONS = [("overlay", "taker"), ("overlay", "maker_first"),
                ("acquire", "taker"), ("acquire", "maker_first")]

needs_corpus = pytest.mark.skipif(
    not os.path.isdir(CORPUS),
    reason="the Bybit settlement corpus is not in this checkout")


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    """One replay per combination. ~0.8s each, so the whole matrix is cheap
    enough to belong in the default suite rather than in a tool nobody runs."""
    folder = tmp_path_factory.mktemp("matrix")
    out = {}
    for mode, style in COMBINATIONS:
        path = str(folder / f"{mode}_{style}.json")
        assert cr.main(["--repo", REPO, "--mode", mode, "--style", style,
                        "--json", path]) == 0
        with open(path, encoding="utf-8") as handle:
            out[(mode, style)] = json.load(handle)
    return out


@needs_corpus
class TestEveryCombinationReconciles:
    @pytest.mark.parametrize("combo", COMBINATIONS)
    def test_the_journal_agrees_with_the_running_totals(self, runs, combo):
        r = runs[combo]
        assert r["ledger_agrees"], (
            f"{combo}: journal {r['ledger_net_usd']:,.2f} + unrealised "
            f"{r['unrealised_usd']:,.2f} != replay {r['net_usd']:,.2f}")

    @pytest.mark.parametrize("combo", COMBINATIONS)
    def test_the_gap_is_zero_to_the_cent(self, runs, combo):
        r = runs[combo]
        gap = r["ledger_net_usd"] + r["unrealised_usd"] - r["net_usd"]
        assert abs(gap) < 0.01, f"{combo}: ${gap:,.2f} unaccounted"

    @pytest.mark.parametrize("combo", COMBINATIONS)
    def test_the_fees_agree_in_magnitude(self, runs, combo):
        """D44b was three orders of magnitude. The signs differ by convention —
        the replay reports fees as a negative contribution to net, the journal
        as a positive expense — so the magnitude is the claim."""
        r = runs[combo]
        assert abs(r["ledger"]["fees_usd"]) == pytest.approx(
            abs(r["fees_usd"]), rel=1e-6)

    @pytest.mark.parametrize("combo", COMBINATIONS)
    def test_the_whole_corpus_was_walked(self, runs, combo):
        """A combination that halted early would reconcile trivially."""
        r = runs[combo]
        assert r["settlements_walked"] == r["settlements"]
        assert r["halted_at_ms"] is None

    @pytest.mark.parametrize("combo", COMBINATIONS)
    def test_fees_are_a_plausible_fraction_of_the_book(self, runs, combo):
        """A standing absurdity check. Fees cannot exceed the notional many
        times over; D44b's $388,719,591 against a $100,000 book trips this on
        its own, without anyone having to know what the right number is."""
        r = runs[combo]
        assert abs(r["ledger"]["fees_usd"]) < 10.0 * r["notional_usd"]

    @pytest.mark.parametrize("combo", COMBINATIONS)
    def test_the_short_leg_is_really_there(self, runs, combo):
        """Whichever way the long leg is accounted for, a delta-neutral book
        has a short leg that realised real money. A run that reconciled
        because it traded nothing would pass everything above."""
        assert abs(runs[combo]["hedge"]["perp_realised_usd"]) > 10_000.0


@needs_corpus
class TestTheRevaluationIsOverlayOnly:
    """D44c. `revalue_inventory` is overlay-scoped by its own docstring: it
    exists because the overlay never TRADES the client's coin, so nothing else
    books that leg. In acquire the book buys the spot and sells it again, so
    `spot_sell` realises it — and a revaluation on top counts the same dollars
    twice. That was worth exactly $151,904.02 before it was guarded."""

    @pytest.mark.parametrize("style", ("taker", "maker_first"))
    def test_overlay_revalues_the_inventory_it_never_trades(self, runs, style):
        assert abs(runs[("overlay", style)]["hedge"]["inventory_change_usd"]) \
            > 1_000.0

    @pytest.mark.parametrize("style", ("taker", "maker_first"))
    def test_acquire_does_not_revalue_what_it_sold(self, runs, style):
        assert runs[("acquire", style)]["hedge"]["inventory_change_usd"] == \
            pytest.approx(0.0)


@needs_corpus
class TestTheMatrixIsThreeRealPathsNotFour:
    """`_fire` passes `patient=True` at exactly one call site — the overlay
    entry — so ACQUIRE NEVER RESTS. Pinning that is the difference between a
    four-case test and a three-case test wearing four."""

    def test_acquire_places_no_maker_orders(self, runs):
        assert runs[("acquire", "maker_first")]["maker_fills"] == 0

    def test_acquire_maker_first_is_identical_to_acquire_taker(self, runs):
        maker = runs[("acquire", "maker_first")]
        taker = runs[("acquire", "taker")]
        assert maker["net_usd"] == pytest.approx(taker["net_usd"])
        assert maker["fees_usd"] == pytest.approx(taker["fees_usd"])

    def test_overlay_is_the_only_path_that_rests(self, runs):
        assert runs[("overlay", "maker_first")]["maker_fills"] > 0


@needs_corpus
class TestTheMakerSavingIsAnUpperBound:
    """It is a FEE saving and it is the most optimistic one obtainable.

    `ReplayBroker.place_post_only` fills EVERY resting order at the touch — a
    100% fill rate by construction, which its own docstring calls the most
    optimistic assumption in the file with no offline evidence behind it.
    MAKER_FIRST_0040 says the same thing in %/yr: +1.36 is the UPPER BOUND,
    the realised value is `fill_rate x 1.36 %/yr`, and the fill rate is
    unmeasured until the drill (INVENTORY D17).

    So these assert the MECHANISM — resting costs less than crossing — and
    deliberately do not pin the number as an achievement.
    """

    def test_resting_costs_less_in_fees_than_crossing(self, runs):
        rested = abs(runs[("overlay", "maker_first")]["fees_usd"])
        crossed = abs(runs[("overlay", "taker")]["fees_usd"])
        assert rested < crossed

    def test_the_saving_is_fees_and_nothing_else(self, runs):
        """Funding is identical; only the fee line moves. If a maker run ever
        shows more FUNDING than a taker run, the saving is not a fee saving
        and the number means something other than what it says."""
        rested = runs[("overlay", "maker_first")]
        crossed = runs[("overlay", "taker")]
        assert rested["funding_usd"] == pytest.approx(crossed["funding_usd"])
        assert rested["trades"] == crossed["trades"]

    def test_every_resting_order_filled_which_is_why_it_is_a_bound(self, runs):
        """One maker fill per trade: the harness never misses. Real queues do."""
        r = runs[("overlay", "maker_first")]
        assert r["maker_fills"] == r["trades"]


@needs_corpus
class TestTheProductNumbersDidNotMove:
    """OVERLAY/taker is what the programme quotes. Nothing above may touch it."""

    def test_overlay_taker_net_is_unchanged(self, runs):
        assert runs[("overlay", "taker")]["net_usd"] == pytest.approx(
            29_766.40, abs=0.01)

    def test_overlay_taker_trade_count_is_unchanged(self, runs):
        assert runs[("overlay", "taker")]["trades"] == 79
