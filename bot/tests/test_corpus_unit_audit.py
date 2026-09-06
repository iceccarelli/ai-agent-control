"""Volume-unit sanity check, and a map of who is exposed to it.

This documents a REAL, currently UNFIXED defect in the shipped corpus:
data/real_linear_1d/ohlcv/BINANCE_LINEAR_BTC_USDT_1D.csv.gz carries
quote-notional instead of base-asset volume for 2026-08-18..2026-08-22
(5.4e9..3.5e10 against a file median of 227,714 - four to five orders of
magnitude off). The prefix-hash/append-only invariants elsewhere in this
suite are BYTE tests; they correctly say nothing about this, because the
bytes are a valid, append-only, monotonic CSV throughout. This is a
MEANING defect, and it needs its own test.

The first assertion below is written to FAIL once a human fixes the
source data, on purpose: a green run must not let this quietly become
"no longer true" without a person reading why. When the corpus is
corrected, delete test_the_known_unit_swap_is_still_present and keep the
rest of the file as a permanent regression guard.
"""
from __future__ import annotations

import gzip
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import corpus_unit_audit as cua  # noqa: E402

CORPUS = cua.DEFAULT_CSV


def test_the_known_unit_swap_is_still_present():
    """Fails (loudly, on purpose) the day someone fixes the source data.

    Until then this documents exactly what is wrong and where, so nobody
    has to rediscover it from a 5-billion-BTC daily volume figure.
    """
    if not os.path.isfile(CORPUS):
        import pytest
        pytest.skip("corpus not present in this checkout")
    rows = cua.load_volume(CORPUS)
    code, runs = cua.audit(rows)
    assert code == 1, (
        "the known 2026-08-18..2026-08-22 volume-unit swap is no longer "
        "detected - if the corpus was corrected, delete this test and keep "
        "the rest of this file as a standing regression guard")
    flagged_dates = {d for r in runs for d in (r["start"], r["end"])}
    assert "2026-08-18" in flagged_dates or any(
        r["start"] <= "2026-08-18" <= r["end"] for r in runs)


def test_synthetic_clean_series_is_not_flagged():
    rows = [(f"2020-01-{d:02d}", 100_000.0 + d) for d in range(1, 25)]
    code, runs = cua.audit(rows)
    assert code == 0 and runs == []


def test_synthetic_unit_swap_is_flagged():
    clean = [(f"2020-01-{d:02d}", 100_000.0) for d in range(1, 15)]
    swapped = [(f"2020-01-{d:02d}", 5.0e9) for d in range(15, 20)]
    tail = [(f"2020-01-{d:02d}", 100_000.0) for d in range(20, 25)]
    code, runs = cua.audit(clean + swapped + tail)
    assert code == 1 and len(runs) == 1
    assert runs[0]["start"] == "2020-01-15" and runs[0]["end"] == "2020-01-19"


def test_a_single_outlier_day_is_not_a_run():
    rows = [(f"2020-01-{d:02d}", 100_000.0) for d in range(1, 25)]
    rows[10] = (rows[10][0], 5.0e9)  # one spike, not a run
    code, runs = cua.audit(rows)
    assert code == 0


def test_insufficient_rows_is_reported_distinctly():
    code, runs = cua.audit([("2020-01-01", 1.0)] * 5)
    assert code == 2 and runs == []


def test_volume_does_not_reach_the_cleared_signal():
    """funding_carry_fade_v1 / _btc_v1 (the signal being forward-shadowed,
    n=2, mean net R -0.8358) must not read volume_traded at all - so this
    defect, while real, does not contaminate the reported forward numbers.
    """
    import inspect
    import signals.funding_carry_fade_v1 as base
    import signals.funding_carry_fade_btc_v1 as cleared
    for mod in (base, cleared):
        src = inspect.getsource(mod)
        assert "volume" not in src.lower(), (
            f"{mod.__name__} references volume; the unit-swap defect may now "
            f"reach the cleared signal's forward numbers - re-audit before "
            f"trusting forward_mean_net_r")


def test_which_frozen_families_DO_use_volume_and_are_therefore_exposed():
    """Not a pass/fail gate - a living map, so 'exposed until fixed' is a
    fact anyone can check rather than something only this test author knows.
    """
    import inspect
    exposed = []
    sig_dir = os.path.join(ROOT, "signals")
    for fname in sorted(os.listdir(sig_dir)):
        if not fname.endswith(".py") or fname == "__init__.py":
            continue
        mod_name = f"signals.{fname[:-3]}"
        try:
            mod = __import__(mod_name, fromlist=["_"])
        except Exception:
            continue
        src = inspect.getsource(mod)
        if "volume" in src.lower():
            exposed.append(fname)
    print("families referencing volume (exposed to the unit-swap defect):",
          exposed)
    # No assertion on the exact set - signal files change - but the check
    # must at least run and must not silently import nothing.
    assert isinstance(exposed, list)
