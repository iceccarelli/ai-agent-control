"""0041 — choose on the past, score on the next month, never look ahead.

WHAT THIS IS FOR
================
`carry_backtest --gated` prints +7.23%/yr and `QUOTABLE: False`, with exactly
one box unticked: `rule_scored_on_an_untouched_window`. The rule's parameters
were chosen after 0018 with the whole corpus visible. Nothing in the number
distinguishes a real carry edge from a well-fitted one, and that is the entire
distance between this book and an asset.

A registered forward holdout is the only thing that ticks the box, and it
needs TIME — settlements that have not happened yet. What can be done today is
the next best evidence: refit at the end of every month using only what had
printed by then, trade the following month with that choice, and never let a
choice see a settlement it then gets scored on.

WHAT IT IS NOT
==============
Not a holdout. Walk-forward reuses the same corpus many times and its result is
not quotable; the box stays unticked and the tool says so on every run. What it
CAN establish is whether the shipped constants are a lucky cell — if a rule
refit blind does about as well as the frozen one, the frozen one was not
bought with hindsight.

THE DEFECT IT ALSO FIXES
========================
The gated rule never reads an entry threshold. `may_open_gated` decides on the
EWMA, the cost of capital and the basis budget; `entry_bps` appears only in the
ungated branch. So 0039's 1,440-cell sweep was 60 distinct gated rules printed
24 times, and the parameters the shipped rule actually has — `ewma_alpha`,
`ewma_min_prints` — were never searched at all. The map had the wrong axes.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import carry_backtest as cb          # noqa: E402
import carry_core as core            # noqa: E402
import carry_walkforward as wf       # noqa: E402

H8 = 8 * 3600 * 1000
native = pytest.mark.skipif(not (core.available() or core.build()),
                            reason="libcarrycore.so is not built here")


def synthetic(n=900, rate=0.0004, start=1_600_000_000_000):
    """A corpus with a regime change halfway: funding pays, then it does not."""
    rows = []
    for i in range(n):
        r = rate if i < n // 2 else -rate
        price = 30_000.0 * (1.0 + 0.0001 * i)
        rows.append(cb.Settlement(ms=start + i * H8, perp=price,
                                  spot=price * (1 - 0.0004), rate=r,
                                  perp_high=price * 1.01, perp_low=price * 0.99))
    return rows


class TestTheAxesAreTheOnesTheRuleActuallyHas:
    @native
    def test_the_gated_rule_never_reads_an_entry_threshold(self):
        """The measurement that makes the axis fix necessary, not an opinion.

        If this ever fails, the gated rule grew an entry threshold and the
        sweep axes must grow one back."""
        rows = synthetic()
        base = wf.base_params(notional=100_000.0, borrow_apr=0.0,
                              impact_bps=1.0, overlay=True)
        nets = {c["net"] for c in core.sweep(
            rows, base, entry_grid=[0.1, 0.5, 1.0, 2.4, 9.9],
            hold_grid=[30.0], exit_grid=[3])}
        assert len(nets) == 1, nets

    def test_entry_bps_is_not_among_the_searched_axes(self):
        assert "entry_bps" not in wf.AXES
        assert set(wf.AXES) == {"hold_days", "negative_exit_prints",
                                "ewma_alpha"}

    def test_the_axes_are_the_gated_rules_free_parameters(self):
        """Every axis must be a name `may_open_gated` or the exit rule reads."""
        import inspect
        cpp = os.path.join(os.path.dirname(__file__), "..", "..", "cpp",
                           "carrycore.cpp")
        source = open(cpp, encoding="utf-8").read() \
            if os.path.exists(cpp) else inspect.getsource(cb)
        for axis in wf.AXES:
            assert axis in source, axis

    def test_the_frozen_cell_is_read_from_the_shipped_constants(self):
        """Not literals. If someone retunes carry_costs, this line moves with
        it and the comparison stays honest."""
        import carry_costs
        import carry_engine
        assert wf.FROZEN["hold_days"] == carry_costs.ASSUMED_HOLD_DAYS
        assert wf.FROZEN["ewma_alpha"] == carry_costs.EWMA_ALPHA
        assert wf.FROZEN["negative_exit_prints"] == \
            carry_engine.NEGATIVE_FUNDING_EXIT_PRINTS


class TestNoChoiceSeesWhatItIsScoredOn:
    def test_a_segment_fits_strictly_before_it_scores(self):
        rows = synthetic(600)
        segs = wf.segments(rows, burn_in_days=100, step_days=30)
        assert segs
        for fit_end, score_end in segs:
            assert fit_end < score_end
        # contiguous, and the first fit is the whole burn-in
        assert segs[0][0] == wf.index_at(rows, rows[0].ms + 100 * 86_400_000)
        for a, b in zip(segs, segs[1:]):
            assert a[1] == b[0]

    @native
    def test_the_future_cannot_change_the_choice(self):
        """The test that would catch a look-ahead bug: rewrite every
        settlement after the fit boundary and the chosen cell must not move."""
        rows = synthetic(600)
        base = wf.base_params(notional=100_000.0, borrow_apr=0.0,
                              impact_bps=1.0, overlay=True)
        fit_end = 300
        first = wf.choose(rows[:fit_end], base)

        poisoned = list(rows)
        for i in range(fit_end, len(poisoned)):
            s = poisoned[i]
            poisoned[i] = cb.Settlement(ms=s.ms, perp=s.perp * 2,
                                        spot=s.spot * 2, rate=s.rate * -5,
                                        perp_high=s.perp_high * 2,
                                        perp_low=s.perp_low * 2)
        assert wf.choose(poisoned[:fit_end], base) == first

    @native
    def test_the_out_of_sample_increment_is_a_difference_of_prefixes(self):
        """P&L for [t1, t2) = net over [0, t2) - net over [0, t1), same
        params. That is what gives the chosen rule credit for the position it
        would already have been holding at t1 under its own history."""
        rows = synthetic(600)
        base = wf.base_params(notional=100_000.0, borrow_apr=0.0,
                              impact_bps=1.0, overlay=True)
        p = wf.cell_params(base, wf.FROZEN)
        whole = core.simulate(rows[:500], p)["net"]
        head = core.simulate(rows[:200], p)["net"]
        assert wf.score(rows, 200, 500, p) == pytest.approx(whole - head,
                                                            abs=1e-6)


class TestWhatItReportsAndWhatItRefusesTo:
    @native
    def test_walk_forward_never_beats_the_oracle(self):
        """The oracle sees everything and picks once. Blind refitting cannot
        do better; if it does, something leaked."""
        r = wf.run_rows(synthetic(900), notional=100_000.0, borrow_apr=0.0,
                        impact_bps=1.0, overlay=True, burn_in_days=100,
                        step_days=30)
        assert r["walk_forward_net"] <= r["oracle_net"] + 1e-6

    @native
    def test_the_frozen_line_is_scored_on_the_same_segments(self):
        """Otherwise the comparison is between two different windows."""
        r = wf.run_rows(synthetic(900), notional=100_000.0, borrow_apr=0.0,
                        impact_bps=1.0, overlay=True, burn_in_days=100,
                        step_days=30)
        assert len(r["segments"]) == len(r["frozen_segment_net"]) \
            == len(r["walk_forward_segment_net"])
        assert sum(r["frozen_segment_net"]) == pytest.approx(r["frozen_net"])

    @native
    def test_it_says_the_result_is_not_quotable(self):
        r = wf.run_rows(synthetic(900), notional=100_000.0, borrow_apr=0.0,
                        impact_bps=1.0, overlay=True, burn_in_days=100,
                        step_days=30)
        assert r["is_a_quotable_return"] is False
        assert "holdout" in r["why_not_quotable"].lower()

    @native
    def test_it_reports_how_wide_the_search_was(self):
        r = wf.run_rows(synthetic(900), notional=100_000.0, borrow_apr=0.0,
                        impact_bps=1.0, overlay=True, burn_in_days=100,
                        step_days=30)
        per_refit = len(wf.HOLD_DAYS) * len(wf.EXIT_PRINTS) * \
            len(wf.EWMA_ALPHA)
        assert r["cells_per_refit"] == per_refit
        assert r["cells_examined"] == per_refit * len(r["segments"])

    @native
    def test_it_names_no_winner_to_ship(self):
        """A walk-forward is evidence about a rule, not a parameter picker.
        If it handed back a config the next commit would paste it into
        carry_costs, which is rule 20 with extra steps."""
        r = wf.run_rows(synthetic(900), notional=100_000.0, borrow_apr=0.0,
                        impact_bps=1.0, overlay=True, burn_in_days=100,
                        step_days=30)
        assert "recommended" not in r
        assert "adopt" not in r

    @native
    def test_a_corpus_too_short_to_walk_is_refused_not_shrunk(self):
        with pytest.raises(SystemExit):
            wf.run_rows(synthetic(30), notional=100_000.0, borrow_apr=0.0,
                        impact_bps=1.0, overlay=True, burn_in_days=365,
                        step_days=30)


class TestTheNumbersItPrintsAreDecompositionsNotEstimates:
    @native
    def test_every_additive_term_telescopes_exactly(self):
        """Summing the segments must reproduce one continuous run, to the bit.
        If it does not, the lines are a stack of little backtests that each
        restart flat, and the churn column is meaningless."""
        rows = synthetic(900)
        base = wf.base_params(notional=100_000.0, borrow_apr=0.0,
                              impact_bps=1.0, overlay=True)
        p = wf.cell_params(base, wf.FROZEN)
        segs = wf.segments(rows, burn_in_days=100, step_days=30)
        head = core.simulate(rows[:segs[-1][1]], p)
        tail = core.simulate(rows[:segs[0][0]], p)
        parts = [wf.segment_result(rows, a, b, p) for a, b in segs]
        for term in wf._ADDITIVE:
            assert sum(x[term] for x in parts) == pytest.approx(
                head[term] - tail[term], abs=1e-6), term

    @native
    def test_it_reports_the_churn_not_only_the_net(self):
        """The net column alone made 57 trades look like 6. Fees, trade count
        and days held are what separate them."""
        r = wf.run_rows(synthetic(900), notional=100_000.0, borrow_apr=0.0,
                        impact_bps=1.0, overlay=True, burn_in_days=100,
                        step_days=30)
        for key in ("frozen", "walk_forward", "oracle"):
            line = r[key]
            for field in ("trades", "fees_usd", "days_in_market",
                          "mean_hold_days", "market_exposure_pct",
                          "median_segment_usd", "top1_share", "top3_share"):
                assert field in line, (key, field)

    @native
    def test_the_frozen_line_is_charged_more_than_one_trial(self):
        """Its constants were chosen after 0018 with this corpus visible.
        Scoring them as if pre-registered would print a flattering z for a
        rule that was, in fact, selected."""
        import carry_walkforward
        source = open(carry_walkforward.__file__, encoding="utf-8").read()
        assert '"frozen_segment_net", 1)' not in source
        assert "UPPER bound" in source


class TestTheSearchWidthReachesTheRegistry:
    def test_the_width_recorded_is_the_distinct_rule_count(self, tmp_path):
        import hypothesis_registry as hr
        path = str(tmp_path / "registry.json")
        hr.save(hr.load(path), path)
        report = {"cells_examined": 5400, "cells_per_refit": 540,
                  "segments": [(1, 2)] * 10, "window": "A .. B",
                  "settlements": 900, "burn_in_days": 365, "step_days": 30}
        wf.record_width(report, path=path)
        blob = open(path, encoding="utf-8").read()
        assert "5400" in blob
        # the inflated 24x entry axis must not appear anywhere
        assert "129600" not in blob


class TestTheSweepLostItsDeadAxis:
    def test_the_sweep_no_longer_searches_entry_when_gated(self):
        import carry_sweep
        assert carry_sweep.ENTRY_BPS_GATED == [0.0]
        assert len(carry_sweep.EWMA_ALPHA) > 1

    def test_the_grid_id_changed_with_the_grid(self):
        """A registry keyed by a grid hash must not reuse the old key for a
        different grid."""
        import carry_sweep
        assert carry_sweep.grid_id() != "36b0f5a3d0b1"
