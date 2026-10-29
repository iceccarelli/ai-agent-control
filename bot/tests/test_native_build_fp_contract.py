"""The native core must not fuse multiply-adds, whatever flags the build inherits.

The C++ gate and simulator promise the same arithmetic as the Python reference.
GCC contracts ``a * b - c`` into one fused multiply-add when the target has FMA
(``-ffp-contract=fast`` is its default outside strict ISO mode), and a fused
operation does not overflow in the middle: ``BIG * 3 - inf`` is ``-inf`` instead
of the ``inf - inf = NaN`` Python computes, so the gate answered
CARRY_BELOW_COST_OF_CAPITAL where it must refuse as COST_INPUT_INVALID. That
failed on a CI runner and not on machines whose default target has no FMA.
Offline; builds into tmp_path only.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
CPP = os.path.join(os.path.dirname(BOT), "cpp")
sys.path.insert(0, os.path.join(BOT, "tools"))
sys.path.insert(0, BOT)

import carry_core as core  # noqa: E402

BIG = 1.7976931348623157e308
HAS_MAKE = shutil.which("make") and shutil.which("g++")


def _dry_run(cxxflags):
    out = subprocess.run(["make", "-n", "-B", "-C", CPP, f"CXXFLAGS={cxxflags}"],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return out.stdout


@pytest.mark.skipif(not HAS_MAKE, reason="no make/g++")
@pytest.mark.parametrize("inherited", ["-O2", "-O3 -march=native",
                                       "-O2 -mfma -ffp-contract=fast"])
def test_the_build_line_always_forbids_fused_multiply_add(inherited):
    line = _dry_run(inherited)
    assert "-ffp-contract=off" in line
    # last flag wins: it must come AFTER whatever the environment inherited
    assert line.rindex("-ffp-contract=off") > line.rindex(inherited.split()[-1])


@pytest.mark.skipif(not HAS_MAKE, reason="no make/g++")
@pytest.mark.skipif(not os.path.exists("/proc/cpuinfo")
                    or " fma" not in open("/proc/cpuinfo").read(),
                    reason="this CPU has no FMA, nothing to contract")
def test_a_build_that_asks_for_fma_still_answers_like_python(tmp_path, monkeypatch):
    lib = tmp_path / "libcarrycore.so"
    built = subprocess.run(
        ["make", "-C", CPP, f"OUT={lib}",
         "CXXFLAGS=-O2 -std=c++17 -fPIC -pthread -mfma"],
        capture_output=True, text=True, timeout=300)
    assert built.returncode == 0, built.stderr
    monkeypatch.setattr(core, "LIB_PATH", str(lib))
    monkeypatch.setattr(core, "_LIB", None)
    p = core.params(borrow_apr=BIG, round_trip_bps=11.0, gated=True, overlay=True)
    out = core.entry_gate([BIG] * 3, 150.0, 100.0, p)
    assert (out["allowed"], out["reason"]) == (False, "COST_INPUT_INVALID")
