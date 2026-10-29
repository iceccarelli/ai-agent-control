"""The native core's int32 parameters must be refused, not silently wrapped, and
a corrupt library must read as 'not built'.

ctypes wraps an out-of-range Python int into a C int32 without error, so
``latency_rows=2**32`` became 0 (the zero-latency upper bound) and
``negative_exit_prints=2**31`` became a negative exit streak. Neither is
capital, but both silently turn a sweep into evidence about a different rule.
``load()`` also let a non-ELF ``libcarrycore.so`` raise ``OSError`` where
``available()`` callers expect ``False``. Offline; no library is needed.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import carry_core as core  # noqa: E402

INT32_MAX = 2**31 - 1


@pytest.mark.parametrize("field", ["latency_rows", "negative_exit_prints",
                                   "funding_history", "ewma_min_prints"])
@pytest.mark.parametrize("value", [2**31, 2**32, 2**40])
def test_int32_parameters_out_of_range_are_refused(field, value):
    with pytest.raises(ValueError):
        core.params(**{field: value})


def test_the_int32_maximum_is_still_accepted():
    assert core.params(latency_rows=INT32_MAX).latency_rows == INT32_MAX


def test_a_sweep_grid_entry_out_of_int32_is_refused():
    with pytest.raises(ValueError):
        core.sweep([], core.params(), entry_grid=[1.0], hold_grid=[1.0],
                   exit_grid=[2**32])


def test_a_corrupt_library_reads_as_not_available(tmp_path, monkeypatch):
    junk = tmp_path / "libcarrycore.so"
    junk.write_bytes(b"not an ELF file")
    monkeypatch.setattr(core, "LIB_PATH", str(junk))
    monkeypatch.setattr(core, "_LIB", None)
    assert core.available() is False
