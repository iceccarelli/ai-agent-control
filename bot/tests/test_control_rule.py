"""Slice 37 — the human-pre-declared control validity rule.

    (a) |z| < 1.96          two-sided, mean of surrogate percentiles vs 50,
                            SE = sample sd / sqrt(n)
    (b) KS vs Uniform(0,100) NOT rejected at p >= 0.05
    (c) incompletes <= 5% of surrogates
    ALL THREE required.

The rule matters more than any one control run, so it is tested against
hand-built percentile vectors rather than only against twenty-minute surrogate
sweeps. Each clause gets a case that isolates it: a vector that fails exactly
that clause and passes the others.

The load-bearing test is `test_a_median_above_fifty_alone_does_not_invalidate`.
`median <= 50` was the gate for twenty slices and is retired here, and the whole
point of the replacement is that a correct instrument stops being failed by a
coin flip.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tools"))

import control_directed as cd  # noqa: E402


def uniform_vector(n=1000, seed=0):
    """A clean uniform sample — what a correct instrument produces."""
    return list(np.random.default_rng(seed).uniform(0.0, 100.0, n))


class TestTheClausesAreTheDeclaredOnes:

    def test_the_constants_match_the_pre_declaration(self):
        assert cd.Z_ABS_MAX == 1.96
        assert cd.KS_MIN_P == 0.05
        assert cd.MAX_INCOMPLETE_SHARE == 0.05

    def test_validity_is_the_conjunction_of_exactly_three_clauses(self):
        verdict = cd.evaluate_control(uniform_vector(), incomplete=0, total=1000)
        assert verdict.valid == (verdict.z_ok and verdict.ks_ok
                                 and verdict.complete_ok)

    def test_the_standard_error_uses_the_sample_sd(self):
        """The pre-declaration says SE = sd/sqrt(n), not the theoretical 28.87."""
        values = uniform_vector(500, seed=3)
        verdict = cd.evaluate_control(values, incomplete=0, total=500)
        import statistics
        expected = (statistics.fmean(values) - 50.0) / (
            statistics.stdev(values) / np.sqrt(500))
        assert verdict.z == pytest.approx(expected, rel=1e-9)


class TestUniformSurrogatesAreValid:

    @pytest.mark.parametrize("seed", range(6))
    def test_a_clean_uniform_vector_passes(self, seed):
        verdict = cd.evaluate_control(uniform_vector(1000, seed),
                                      incomplete=0, total=1000)
        assert verdict.valid, verdict.failures

    def test_it_passes_at_the_minimum_sample_size(self):
        verdict = cd.evaluate_control(uniform_vector(200, seed=11),
                                      incomplete=0, total=200)
        assert verdict.valid, verdict.failures


class TestBiasedSurrogatesAreInvalid:

    def test_a_location_shifted_vector_fails_on_z(self):
        """Every percentile pushed up by 10 — the dangerous direction."""
        values = [min(100.0, v + 10.0) for v in uniform_vector(1000, seed=1)]
        verdict = cd.evaluate_control(values, incomplete=0, total=1000)
        assert verdict.z_ok is False
        assert verdict.valid is False
        assert any("(a)" in f for f in verdict.failures)

    def test_a_wrong_shape_with_the_right_mean_fails_on_ks(self):
        """Mean 50, but the mass is at the ends. A z test alone cannot see this.

        This is exactly what clause (b) is for, and exactly what the retired
        median clause could never have caught either.
        """
        rng = np.random.default_rng(2)
        values = list(np.where(rng.random(1000) < 0.5,
                               rng.uniform(0, 5, 1000),
                               rng.uniform(95, 100, 1000)))
        verdict = cd.evaluate_control(values, incomplete=0, total=1000)
        assert verdict.z_ok is True          # mean is ~50
        assert verdict.ks_ok is False        # shape is not uniform
        assert verdict.valid is False
        assert any("(b)" in f for f in verdict.failures)

    def test_a_narrow_concentration_around_fifty_also_fails_on_ks(self):
        """Too-good-to-be-true is a defect too, not a triumph."""
        values = list(np.random.default_rng(4).normal(50.0, 3.0, 1000))
        verdict = cd.evaluate_control(values, incomplete=0, total=1000)
        assert verdict.ks_ok is False
        assert verdict.valid is False

    def test_an_all_hundred_vector_fails(self):
        verdict = cd.evaluate_control([100.0] * 500, incomplete=0, total=500)
        assert verdict.valid is False


class TestIncompletesAreAHardClause:

    def test_over_five_percent_invalidates_even_when_z_and_ks_pass(self):
        """The decisive case for clause (c)."""
        values = uniform_vector(1000, seed=5)
        clean = cd.evaluate_control(values, incomplete=0, total=1000)
        assert clean.valid is True                      # control

        verdict = cd.evaluate_control(values, incomplete=51, total=1000)
        assert verdict.z_ok is True
        assert verdict.ks_ok is True
        assert verdict.complete_ok is False
        assert verdict.valid is False
        assert any("(c)" in f for f in verdict.failures)

    def test_exactly_five_percent_is_allowed(self):
        values = uniform_vector(1000, seed=6)
        verdict = cd.evaluate_control(values, incomplete=50, total=1000)
        assert verdict.complete_ok is True

    def test_no_usable_surrogates_is_invalid(self):
        verdict = cd.evaluate_control([], incomplete=200, total=200)
        assert verdict.valid is False
        assert verdict.n == 0


class TestTheMedianIsRetired:
    """The point of the replacement, asserted directly."""

    def test_a_median_above_fifty_alone_does_not_invalidate(self):
        """A correct instrument must stop being failed by a coin flip.

        Constructed so the median sits clearly above 50 while the mean, the
        shape and the completeness are all fine. Under the retired rule this
        was INVALID; under the declared rule it is VALID.
        """
        rng = np.random.default_rng(7)
        for seed_try in range(200):
            values = list(rng.uniform(0.0, 100.0, 400))
            import statistics
            if statistics.median(values) > 50.0:
                verdict = cd.evaluate_control(values, incomplete=0, total=400)
                if verdict.valid:
                    assert statistics.median(values) > 50.0
                    assert verdict.median > 50.0
                    return
        pytest.fail("no uniform sample with median > 50 was found in 200 tries")

    def test_the_median_is_reported_but_is_not_a_clause(self):
        verdict = cd.evaluate_control(uniform_vector(500, seed=8),
                                      incomplete=0, total=500)
        assert hasattr(verdict, "median")
        # The verdict is reproduced exactly by the three clauses alone.
        assert verdict.valid == (verdict.z_ok and verdict.ks_ok
                                 and verdict.complete_ok)

    def test_no_median_comparison_decides_validity_in_the_source(self):
        """Structural: `median` must not appear in any boolean the gate uses."""
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(cd.evaluate_control))
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare):
                names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
                assert "median" not in names, ast.dump(node)[:120]

    def test_the_runner_prints_the_median_as_informational(self):
        import inspect
        source = inspect.getsource(cd.main)
        assert "INFORMATIONAL ONLY" in source


class TestTheEmittedLineIsMachineReadable:
    """The registration hook greps for this exact string."""

    def test_valid_and_invalid_markers_are_the_expected_literals(self):
        import inspect
        source = inspect.getsource(cd.main)
        assert 'CONTROL: **VALID**' in source
        assert 'CONTROL: **INVALID' in source

    def test_the_attestation_string_matches_what_edge_measurement_looks_for(self):
        import inspect
        import edge_measurement as em
        assert 'CONTROL: **VALID**' in inspect.getsource(em.main)

    def test_an_invalid_log_cannot_attest(self, tmp_path):
        """A log that says INVALID must not satisfy the attestation grep."""
        path = tmp_path / "bad.log"
        path.write_text("CONTROL: **INVALID — the directed instrument ...**\n")
        assert "CONTROL: **VALID**" not in path.read_text()


class TestTheSlice39ControlLogsCannotAttest:
    """Slice 39 — the human's one-day exclusion did not move the control.

    The exclusion of 2025-01-07 was applied to both paths and the control
    reproduced slice 38 to the digit: ETH z +2.118, SOL z +2.165, and all 1,000
    surrogate percentiles per symbol identical. EDGE.md 20b predicted exactly
    that, in writing, before the run — the flag arrays are unchanged, so the
    inputs were unchanged.

    These logs must stay unusable as attestations for the same reason the
    slice-38 ones are.
    """

    LOGS = ("artifacts/slice39_control_directed_ETHUSDT_n1000.log",
            "artifacts/slice39_control_directed_SOLUSDT_n1000.log")

    @pytest.mark.parametrize("name", LOGS)
    def test_the_log_says_invalid_and_cannot_attest(self, name):
        text = open(os.path.join(REPO, name), encoding="utf-8").read()
        assert "CONTROL: **INVALID" in text
        assert "CONTROL: **VALID**" not in text

    @pytest.mark.parametrize("name", LOGS)
    def test_the_log_records_the_exclusion_it_ran_under(self, name):
        """A control log that does not say which driver it used is not evidence."""
        text = open(os.path.join(REPO, name), encoding="utf-8").read()
        assert "3,134 bars used" in text
        assert "1 excluded: 2025-01-07" in text

    @pytest.mark.parametrize("symbol", ["ETHUSDT", "SOLUSDT"])
    def test_slice_39_reproduces_slice_38_exactly(self, symbol):
        """The prediction in EDGE.md 20b, asserted against the artefacts.

        Identical inputs and an identical seed must give an identical answer.
        If this ever fails, either the exclusion started doing something or the
        run stopped being deterministic — and both are defects, not results.
        """
        def verdict(slice_name):
            path = os.path.join(
                REPO, "artifacts",
                f"{slice_name}_control_directed_{symbol}_n1000.log")
            text = open(path, encoding="utf-8").read()
            return text[text.index("surrogates used"):]

        assert verdict("slice39") == verdict("slice38")

    def test_no_slice_39_edge_artefact_was_produced(self):
        import glob
        assert glob.glob(os.path.join(REPO, "artifacts",
                                      "slice39_edge_*")) == []


class TestTheSlice38ControlLogsCannotAttest:
    """Slice 38 — the full-window controls FAILED, and must stay unusable.

    On 890 dates both symbols passed (z +0.372, +0.287). On the full 1,461
    they do not (z +2.118, +2.165), and SOL fails the KS clause too. The
    registration hook only accepts a log containing `CONTROL: **VALID**`, so
    these logs must never satisfy it — a future session that finds them on
    disk and attaches one as an attestation has to fail here first.
    """

    LOGS = ("artifacts/slice38_control_directed_ETHUSDT_n1000.log",
            "artifacts/slice38_control_directed_SOLUSDT_n1000.log")

    @pytest.mark.parametrize("name", LOGS)
    def test_the_log_says_invalid_and_cannot_attest(self, name):
        path = os.path.join(REPO, name)
        assert os.path.exists(path), name
        text = open(path, encoding="utf-8").read()
        assert "CONTROL: **INVALID" in text
        assert "CONTROL: **VALID**" not in text

    @pytest.mark.parametrize("name", LOGS)
    def test_the_failing_clause_is_named_in_the_log(self, name):
        """A verdict without its reason is not reviewable."""
        text = open(os.path.join(REPO, name), encoding="utf-8").read()
        assert "failed:" in text
        assert "(a)" in text

    @pytest.mark.parametrize("name", LOGS)
    def test_the_log_forbids_reporting_a_real_series_percentile(self, name):
        text = open(os.path.join(REPO, name), encoding="utf-8").read()
        assert "No real-series percentile" in text

    def test_no_slice_38_edge_artefact_was_produced(self):
        """The control ran first precisely so this directory stays empty."""
        import glob
        produced = glob.glob(os.path.join(REPO, "artifacts",
                                          "slice38_edge_*"))
        assert produced == [], produced
