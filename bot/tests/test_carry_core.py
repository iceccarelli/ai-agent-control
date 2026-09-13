"""0039 — the native core: same arithmetic, measured speed, no order path.

Measured on this tree BEFORE the C++ was written:

    one engine decision           1.8 us      the book decides 3x a day
    one 4-year settlement sim     5 ms        4,500 prints

So a native core buys nothing on the decision path, and these tests do not
pretend otherwise. What it buys is SEARCH: 1,440 configurations in 43 ms
instead of 8 seconds, which is the difference between asking a question and
scheduling one.

The only thing that makes a fast simulator usable is that it agrees with the
slow one. Every number below is compared to tools/carry_backtest.py to the
cent.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import carry_backtest as cb  # noqa: E402
import carry_core as core  # noqa: E402

REPO = os.path.join(os.path.dirname(__file__), "..")
CENT = 0.01


@pytest.fixture(scope="module")
def native():
    """Build on demand. No compiler is a skip, never a silent Python fallback
    pretending to be fast."""
    if not core.available() and not core.build():
        pytest.skip("no compiler and no libcarrycore.so")
    return core


@pytest.fixture(scope="module")
def rows():
    return cb.load_bybit_settlements(REPO)[0]


CONFIGS = [
    ("overlay gated 0%", dict(gated=True, overlay=True, borrow_apr=0.0)),
    ("overlay gated 5%", dict(gated=True, overlay=True, borrow_apr=0.05)),
    ("overlay ungated", dict(gated=False, overlay=True, borrow_apr=0.0)),
    ("acquire gated 5%", dict(gated=True, overlay=False, borrow_apr=0.05)),
    ("acquire ungated 3%", dict(gated=False, overlay=False, borrow_apr=0.03)),
    ("no impact", dict(gated=True, overlay=True, borrow_apr=0.0, impact=0.0)),
]


def _both(rows, *, gated, overlay, borrow_apr, impact=1.093, notional=100_000.0):
    mode = cb.OVERLAY if overlay else cb.ACQUIRE
    python = cb.simulate_settlements_series(
        rows, notional=notional, borrow_apr=borrow_apr, gated=gated,
        impact_bps=impact, mode=mode)
    params = core.params(
        notional=notional, borrow_apr=borrow_apr, impact_bps=impact,
        round_trip_bps=cb.round_trip_bps_for(mode), gated=gated,
        overlay=overlay, taker_spot_bps=cb.TAKER_BPS_SPOT,
        taker_perp_bps=cb.TAKER_BPS_PERP)
    return python, core.simulate(rows, params)


class TestItAgreesWithThePythonToTheCent:
    @pytest.mark.parametrize("name,config", CONFIGS, ids=[c[0] for c in CONFIGS])
    def test_every_term_matches(self, native, rows, name, config):
        python, cpp = _both(rows, **config)
        attribution = python["attribution"]
        assert cpp["net"] == pytest.approx(attribution["net_usd"], abs=CENT), name
        assert cpp["funding"] == pytest.approx(attribution["funding_usd"], abs=CENT)
        assert cpp["basis"] == pytest.approx(attribution["basis_usd"], abs=CENT)
        assert cpp["fees"] == pytest.approx(-attribution["fees_usd"], abs=CENT)
        assert cpp["borrow"] == pytest.approx(-attribution["borrow_usd"], abs=CENT)
        assert cpp["impact"] == pytest.approx(-attribution["impact_usd"], abs=CENT)

    @pytest.mark.parametrize("name,config", CONFIGS, ids=[c[0] for c in CONFIGS])
    def test_the_same_trades_happen(self, native, rows, name, config):
        python, cpp = _both(rows, **config)
        assert cpp["trades"] == python["trades"], name
        assert cpp["closed_trades"] == python["closed_trades"]
        assert cpp["losing_trades"] == python["losing_trades"]

    def test_the_exit_rule_counts_prints_in_both(self, native, rows):
        """Three negative PRINTS, not ticks and not days — the thing 0034
        fixed in the engine has to be true here too."""
        python, cpp = _both(rows, gated=False, overlay=True, borrow_apr=0.0)
        assert cpp["trades"] == python["trades"] == 87

    def test_the_worst_excursion_matches(self, native, rows):
        python, cpp = _both(rows, gated=True, overlay=True, borrow_apr=0.0)
        assert cpp["max_adverse_short_pct"] == pytest.approx(
            python["max_adverse_short_excursion_pct"], abs=1e-6)

    def test_it_is_deterministic(self, native, rows):
        first = _both(rows, gated=True, overlay=True, borrow_apr=0.0)[1]
        second = _both(rows, gated=True, overlay=True, borrow_apr=0.0)[1]
        assert first == second


class TestTheSweepIsTheSameSimulation:
    def test_each_cell_equals_a_single_run(self, native, rows):
        base = core.params(notional=100_000.0, borrow_apr=0.0,
                           impact_bps=1.093, round_trip_bps=11.0, gated=True,
                           overlay=True)
        entry, hold, exits = [0.2, 0.9], [7.0, 30.0], [2, 3]
        cells = native.sweep(rows, base, entry_grid=entry, hold_grid=hold,
                             exit_grid=exits)
        assert len(cells) == 8
        for cell in cells:
            one = core.params(
                notional=100_000.0, borrow_apr=0.0, impact_bps=1.093,
                round_trip_bps=11.0, gated=True, overlay=True,
                entry_bps=cell["entry_bps"], hold_days=cell["hold_days"],
                negative_exit_prints=cell["negative_exit_prints"])
            alone = core.simulate(rows, one)
            assert cell["net"] == alone["net"]
            assert cell["trades"] == alone["trades"]

    def test_the_grid_is_ordered_entry_then_hold_then_exit(self, native, rows):
        base = core.params(gated=True, overlay=True)
        cells = native.sweep(rows, base, entry_grid=[0.1, 0.2],
                             hold_grid=[7.0, 30.0], exit_grid=[1, 3])
        assert [(c["entry_bps"], c["hold_days"], c["negative_exit_prints"])
                for c in cells] == [
            (0.1, 7.0, 1), (0.1, 7.0, 3), (0.1, 30.0, 1), (0.1, 30.0, 3),
            (0.2, 7.0, 1), (0.2, 7.0, 3), (0.2, 30.0, 1), (0.2, 30.0, 3)]

    def test_threads_do_not_change_the_answer(self, native, rows):
        base = core.params(gated=True, overlay=True, impact_bps=1.0)
        one = native.sweep(rows, base, entry_grid=[0.2, 0.6],
                           hold_grid=[7.0, 30.0], exit_grid=[3], threads=1)
        many = native.sweep(rows, base, entry_grid=[0.2, 0.6],
                            hold_grid=[7.0, 30.0], exit_grid=[3], threads=8)
        assert [c["net"] for c in one] == [c["net"] for c in many]


class TestItIsActuallyFaster:
    def test_the_sweep_beats_python_by_at_least_twenty_times(self, native, rows):
        """Not a benchmark for its own sake: if the core is not much faster
        there is no reason to carry C++ in this repository at all."""
        base = core.params(gated=True, overlay=True, impact_bps=1.093,
                           round_trip_bps=11.0)
        entry = [0.1 * i for i in range(1, 13)]
        started = time.perf_counter()
        cells = native.sweep(rows, base, entry_grid=entry, hold_grid=[7.0, 30.0],
                             exit_grid=[1, 3, 6])
        native_per_run = (time.perf_counter() - started) / len(cells)

        started = time.perf_counter()
        for value in entry[:3]:
            cb.simulate_settlements_series(rows, notional=100_000.0,
                                           borrow_apr=0.0, gated=True,
                                           impact_bps=1.093, mode=cb.OVERLAY,
                                           entry_bps=value)
        python_per_run = (time.perf_counter() - started) / 3
        assert python_per_run / native_per_run > 20.0, (
            f"only {python_per_run / native_per_run:.1f}x faster")


class TestItCannotReachMoney:
    def test_the_core_has_no_order_path(self):
        with open(os.path.join(REPO, "..", "cpp", "carrycore.cpp"),
                  encoding="utf-8") as fh:
            body = fh.read()
        # Symbols, not English: the file says "in the same ORDER" about
        # arithmetic, which is the opposite of an order path.
        for banned in ("place_order", "place_market", "http", "curl",
                       "socket", "connect(", "send(", "api_key", "secret",
                       "#include <net", "fopen", "system("):
            assert banned not in body.lower(), banned
        assert "extern \"C\"" in body

    def test_the_wrapper_has_no_order_path(self):
        with open(os.path.join(REPO, "tools", "carry_core.py"),
                  encoding="utf-8") as fh:
            body = fh.read()
        for banned in ("place_market", "place_order", "live_authorized",
                       "_request("):
            assert banned not in body

    def test_it_is_one_translation_unit_and_no_dependencies(self):
        cpp = os.path.join(REPO, "..", "cpp")
        assert sorted(os.listdir(cpp)) == ["Makefile", "carrycore.cpp",
                                           "carrycore.h"]
        with open(os.path.join(cpp, "carrycore.cpp"), encoding="utf-8") as fh:
            includes = [l for l in fh if l.startswith("#include")]
        assert all("<" in line or "carrycore.h" in line for line in includes)


class TestThePythonStillWorksWithoutIt:
    def test_the_wrapper_refuses_rather_than_pretending(self, monkeypatch):
        monkeypatch.setattr(core, "_LIB", None)
        monkeypatch.setattr(core, "LIB_PATH", "/nonexistent/libcarrycore.so")
        assert core.available() is False
        with pytest.raises(RuntimeError, match="not built"):
            core.simulate([], core.params())

    def test_the_backtest_never_imports_the_core(self):
        """carry_backtest is the reference implementation. If it started
        calling the core, the differential test would compare the core with
        itself."""
        with open(os.path.join(REPO, "tools", "carry_backtest.py"),
                  encoding="utf-8") as fh:
            assert "carry_core" not in fh.read()


class TestTheSweepRefusesToPickAWinner:
    def test_it_refuses_without_a_holdout(self, native, capsys):
        import carry_sweep
        assert carry_sweep.main(["--repo", REPO, "--top", "1", "--best"]) == 1
        assert "REFUSED" in capsys.readouterr().err

    def test_it_refuses_a_window_something_has_read(self, native, capsys):
        import carry_sweep
        code = carry_sweep.main(["--repo", REPO, "--top", "1", "--best",
                                 "--holdout-from", "2025-01-01T00:00:00Z"])
        err = capsys.readouterr().err
        assert code == 1
        assert "recorded read" in err
        assert "FORWARD" in err

    def test_the_map_says_it_is_in_sample(self, native, capsys):
        import carry_sweep
        assert carry_sweep.main(["--repo", REPO, "--top", "3"]) == 0
        out = capsys.readouterr().out
        assert "IN-SAMPLE MAP, NOT A CHOICE" in out
        assert "No cell is chosen" in out

    def test_the_search_width_is_registered(self, native, tmp_path):
        import carry_sweep
        import hypothesis_registry as hr
        path = str(tmp_path / "registry.json")
        report = carry_sweep.run(REPO, notional=100_000.0, borrow_apr=0.0,
                                 overlay=True)
        trial = carry_sweep.record_width(report, path=path)
        trials = hr.load(path)["trials"]
        assert [t["trial_id"] for t in trials] == [trial]
        assert str(len(report["cells"])) in trials[0]["note"]
        assert "IN-SAMPLE" in trials[0]["note"]
