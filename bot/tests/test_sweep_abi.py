"""`carry_sweep` must not abort the process on a thread count or grid it cannot
honour.

Found by driving the C++ ABI with hostile arguments (not reachable from the
default `threads=0`, but `tools/carry_core.sweep(..., threads=N)` passes N
straight through):

  * `threads` is only clamped to the number of configurations, so a sweep of
    100,000 configurations with threads=100,000 asks the OS for 100,000
    threads. `std::thread` throws `std::system_error` when it cannot, nothing
    catches it across the `extern "C"` boundary, and the process dies with
    SIGABRT (`terminate called after throwing an instance of
    'std::system_error'`) taking every result with it.
  * `n_entry * n_hold * n_exit` was computed in `int32_t`: a grid whose product
    reaches 2**32 wraps to 0, the chunk size becomes a division by zero (SIGFPE),
    and one reaching 2**31 wraps negative.

A research sweep is not capital, but a crash that loses a long run is still a
defect, and "hostile counts return 0 / still answer" is the ABI contract the
other entry points already keep. Each case runs in a child process so a crash is
a return code, not a dead test run.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, BOT)
sys.path.insert(0, os.path.join(BOT, "tools"))

import carry_core as core  # noqa: E402


@pytest.fixture(scope="module")
def native():
    built = core.build()
    if not built and not core.available():
        if os.environ.get("REQUIRE_NATIVE_CORE") == "1":
            pytest.fail("REQUIRE_NATIVE_CORE=1 and libcarrycore.so is not built")
        pytest.skip("no compiler and no libcarrycore.so")
    return core


def child(body):
    code = textwrap.dedent(f"""
        import ctypes, sys
        sys.path.insert(0, {BOT!r}); sys.path.insert(0, {os.path.join(BOT, 'tools')!r})
        import carry_core as core, carry_backtest as cb
        H8 = 8 * 3600 * 1000
        rows = [cb.Settlement(ms=i * H8, perp=1e5, spot=1e5, rate=1e-4,
                              perp_high=1e5, perp_low=1e5) for i in range(20)]
        base = core.params(gated=True, overlay=True, round_trip_bps=11.0)
    """) + textwrap.dedent(body)
    return subprocess.run([sys.executable, "-I", "-c", code], capture_output=True,
                          text=True, timeout=300)


@pytest.mark.parametrize("threads", [8, 100_000, 2**31 - 1])
def test_a_huge_thread_count_still_returns_every_result(native, threads):
    out = child(f"""
        res = core.sweep(rows, base, entry_grid=[0.2] * 100_000, hold_grid=[30.0],
                         exit_grid=[3], threads={threads})
        print(len(res), len({{round(r["net"], 6) for r in res}}))
    """)
    assert out.returncode == 0, f"crashed: rc={out.returncode} {out.stderr[-200:]}"
    assert out.stdout.split() == ["100000", "1"]


@pytest.mark.parametrize("dims", [(65536, 65536, 1), (65536, 32768, 1),
                                  (2000, 2000, 2000)])
def test_a_grid_whose_size_overflows_int32_is_refused_not_wrapped(native, dims):
    n_entry, n_hold, n_exit = dims
    out = child(f"""
        D = ctypes.c_double * 1
        entries, holds = D(0.2), D(30.0)
        exits = (ctypes.c_int32 * 1)(3)
        rows_c = core._rows(rows)
        res = (core.Result * 1)()
        ran = core.load().carry_sweep(rows_c, len(rows), ctypes.byref(base),
                                      entries, {n_entry}, holds, {n_hold},
                                      exits, {n_exit}, res, 4)
        print(ran)
    """)
    assert out.returncode == 0, f"crashed: rc={out.returncode} {out.stderr[-200:]}"
    assert out.stdout.strip() == "0", "an unrepresentable grid must be refused"


def test_an_ordinary_sweep_is_unchanged(native):
    out = child("""
        res = core.sweep(rows, base, entry_grid=[0.1, 0.2], hold_grid=[5.0, 30.0],
                         exit_grid=[1, 3], threads=0)
        print(len(res))
    """)
    assert out.returncode == 0 and out.stdout.strip() == "8"
