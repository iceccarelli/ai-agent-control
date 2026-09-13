#!/usr/bin/env python3
"""The native settlement-clock core: same arithmetic, three orders faster.

WHAT IT IS FOR, MEASURED FIRST
==============================
Before a line of C++ was written, on this tree:

    one engine decision           1.8 us      (the book decides 3x a day)
    one 4-year settlement sim     5 ms        (4,500 prints)

A native core buys NOTHING on the decision path. An 8-hour funding clock does
not care about microseconds, and a book that holds for five days has no use
for a faster decision.

What Python is too slow for is SEARCH. Every open question is a sweep over
thousands of configurations: which exit rule survives out of sample, what a
maker fill does to the round trip, where impact eats the edge at size, what
the wick does to the short leg at tick resolution. Those are 10^4-10^7
simulations; at 5 ms each, Python turns a morning's question into a week.

So this is a THROUGHPUT engine. It places no orders, reads no venue, keeps no
state between calls, and cannot reach anything that loses money. It must agree
with tools/carry_backtest.py TO THE CENT, and tests/test_carry_core.py fails
the build if it does not.

    python3 tools/carry_core.py --build
    python3 tools/carry_core.py --bench
"""
from __future__ import annotations

import argparse
import ctypes
import os
import subprocess
import sys
from typing import Any, Dict, List, Optional, Sequence

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                 # bot/
CPP_DIR = os.path.join(os.path.dirname(ROOT), "cpp")
LIB_PATH = os.path.join(ROOT, "lib", "libcarrycore.so")


class Settlement(ctypes.Structure):
    _fields_ = [("ms", ctypes.c_int64), ("perp", ctypes.c_double),
                ("spot", ctypes.c_double), ("rate", ctypes.c_double),
                ("perp_high", ctypes.c_double)]


class Params(ctypes.Structure):
    _fields_ = [("notional", ctypes.c_double), ("entry_bps", ctypes.c_double),
                ("borrow_apr", ctypes.c_double), ("impact_bps", ctypes.c_double),
                ("round_trip_bps", ctypes.c_double),
                ("hold_days", ctypes.c_double),
                ("taker_spot_bps", ctypes.c_double),
                ("taker_perp_bps", ctypes.c_double),
                ("ewma_alpha", ctypes.c_double),
                ("gated", ctypes.c_int), ("overlay", ctypes.c_int),
                ("ewma_min_prints", ctypes.c_int),
                ("negative_exit_prints", ctypes.c_int),
                ("funding_history", ctypes.c_int)]


class Result(ctypes.Structure):
    _fields_ = [("funding", ctypes.c_double), ("basis", ctypes.c_double),
                ("fees", ctypes.c_double), ("borrow", ctypes.c_double),
                ("impact", ctypes.c_double), ("net", ctypes.c_double),
                ("days_in_market", ctypes.c_double),
                ("max_adverse_short_pct", ctypes.c_double),
                ("trades", ctypes.c_int32), ("closed_trades", ctypes.c_int32),
                ("losing_trades", ctypes.c_int32),
                ("refusals", ctypes.c_int32)]

    def as_dict(self) -> Dict[str, Any]:
        return {name: getattr(self, name) for name, _t in self._fields_}


_LIB: Optional[ctypes.CDLL] = None


def build(quiet: bool = True) -> bool:
    """Compile the core. Returns False when there is no compiler — the caller
    then runs the Python simulator and SAYS SO; it never pretends to be fast."""
    try:
        result = subprocess.run(["make", "-C", CPP_DIR], capture_output=True,
                                text=True, timeout=300)
    except (OSError, subprocess.SubprocessError):
        return False
    if not quiet and result.stdout:
        print(result.stdout.strip())
    if result.returncode != 0:
        if not quiet:
            print(result.stderr.strip(), file=sys.stderr)
        return False
    return os.path.exists(LIB_PATH)


def load() -> Optional[ctypes.CDLL]:
    global _LIB
    if _LIB is not None:
        return _LIB
    if not os.path.exists(LIB_PATH):
        return None
    lib = ctypes.CDLL(LIB_PATH)
    lib.carry_simulate.argtypes = [ctypes.POINTER(Settlement), ctypes.c_int32,
                                   ctypes.POINTER(Params),
                                   ctypes.POINTER(Result)]
    lib.carry_simulate.restype = ctypes.c_int
    lib.carry_sweep.argtypes = [
        ctypes.POINTER(Settlement), ctypes.c_int32, ctypes.POINTER(Params),
        ctypes.POINTER(ctypes.c_double), ctypes.c_int32,
        ctypes.POINTER(ctypes.c_double), ctypes.c_int32,
        ctypes.POINTER(ctypes.c_int32), ctypes.c_int32,
        ctypes.POINTER(Result), ctypes.c_int32]
    lib.carry_sweep.restype = ctypes.c_int32
    lib.carrycore_version.restype = ctypes.c_char_p
    _LIB = lib
    return lib


def available() -> bool:
    return load() is not None


def version() -> str:
    lib = load()
    return lib.carrycore_version().decode() if lib else "not built"


def _rows(settlements: Sequence[Any]):
    array = (Settlement * len(settlements))()
    for i, s in enumerate(settlements):
        array[i] = Settlement(int(s.ms), float(s.perp), float(s.spot),
                              float(s.rate), float(s.perp_high))
    return array


def params(*, notional: float = 100_000.0, entry_bps: float = 0.2,
           borrow_apr: float = 0.0, impact_bps: float = 0.0,
           round_trip_bps: float = 31.0, hold_days: float = 30.0,
           taker_spot_bps: float = 10.0, taker_perp_bps: float = 5.5,
           ewma_alpha: float = 0.5, gated: bool = False,
           overlay: bool = False, ewma_min_prints: int = 3,
           negative_exit_prints: int = 3,
           funding_history: int = 8) -> Params:
    return Params(notional, entry_bps, borrow_apr, impact_bps, round_trip_bps,
                  hold_days, taker_spot_bps, taker_perp_bps, ewma_alpha,
                  int(bool(gated)), int(bool(overlay)), ewma_min_prints,
                  negative_exit_prints, funding_history)


def simulate(settlements: Sequence[Any], p: Params) -> Dict[str, Any]:
    lib = load()
    if lib is None:
        raise RuntimeError(
            "libcarrycore.so is not built. Run `python3 tools/carry_core.py "
            "--build`, or use tools/carry_backtest.py, which is the same "
            "arithmetic in Python.")
    rows = _rows(settlements)
    out = Result()
    code = lib.carry_simulate(rows, len(settlements), ctypes.byref(p),
                              ctypes.byref(out))
    if code != 0:
        raise RuntimeError(f"carry_simulate refused the input (code {code})")
    return out.as_dict()


def sweep(settlements: Sequence[Any], base: Params, *,
          entry_grid: Sequence[float], hold_grid: Sequence[float],
          exit_grid: Sequence[int], threads: int = 0) -> List[Dict[str, Any]]:
    """Every combination, in (entry, hold, exit) order. One call, all cores."""
    lib = load()
    if lib is None:
        raise RuntimeError("libcarrycore.so is not built")
    rows = _rows(settlements)
    entries = (ctypes.c_double * len(entry_grid))(*[float(x) for x in entry_grid])
    holds = (ctypes.c_double * len(hold_grid))(*[float(x) for x in hold_grid])
    exits = (ctypes.c_int32 * len(exit_grid))(*[int(x) for x in exit_grid])
    total = len(entry_grid) * len(hold_grid) * len(exit_grid)
    out = (Result * total)()
    ran = lib.carry_sweep(rows, len(settlements), ctypes.byref(base),
                          entries, len(entry_grid), holds, len(hold_grid),
                          exits, len(exit_grid), out, int(threads))
    results = []
    for index in range(ran):
        entry = index // (len(hold_grid) * len(exit_grid))
        rest = index % (len(hold_grid) * len(exit_grid))
        row = out[index].as_dict()
        row.update({"entry_bps": entry_grid[entry],
                    "hold_days": hold_grid[rest // len(exit_grid)],
                    "negative_exit_prints": exit_grid[rest % len(exit_grid)]})
        results.append(row)
    return results


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--bench", action="store_true",
                    help="time the same simulation in both implementations")
    ap.add_argument("--repo", default=ROOT)
    args = ap.parse_args(argv)

    if args.build:
        ok = build(quiet=False)
        print(f"{'built' if ok else 'NOT BUILT'}: {LIB_PATH}")
        print(version())
        return 0 if ok else 1

    if args.bench:
        import time
        sys.path.insert(0, HERE)
        import carry_backtest as cb
        if not available() and not build():
            print("no compiler and no library: nothing to benchmark. The "
                  "Python simulator in tools/carry_backtest.py is the same "
                  "arithmetic.")
            return 1
        rows, _meta = cb.load_bybit_settlements(args.repo)
        p = params(notional=100_000.0, borrow_apr=0.0, impact_bps=1.093,
                   round_trip_bps=11.0, gated=True, overlay=True)

        t0 = time.perf_counter()
        py = cb.simulate_settlements_series(rows, notional=100_000.0,
                                            borrow_apr=0.0, gated=True,
                                            impact_bps=1.093, mode="overlay")
        py_s = time.perf_counter() - t0

        simulate(rows, p)                       # warm the pages
        t0 = time.perf_counter()
        runs = 50
        for _ in range(runs):
            native = simulate(rows, p)
        cc_s = (time.perf_counter() - t0) / runs

        print(f"{version()}   {len(rows)} settlements")
        print(f"  python  {py_s * 1000:8.2f} ms/run   net {py['net_usd']:+,.2f}")
        print(f"  c++     {cc_s * 1000:8.2f} ms/run   net {native['net']:+,.2f}")
        print(f"  speedup {py_s / cc_s:8.1f}x        difference "
              f"${abs(py['net_usd'] - native['net']):.4f}")
        print("  (a single call is dominated by marshalling 4,500 structs "
              "across the ABI — the number that matters is the sweep, which "
              "marshals once)")

        entry = [round(0.1 * i, 2) for i in range(1, 25)]
        hold = [5.0, 7.0, 10.0, 14.0, 20.0, 30.0, 45.0, 60.0, 90.0, 120.0,
                180.0, 240.0]
        exits = [1, 2, 3, 4, 6]
        total = len(entry) * len(hold) * len(exits)
        t0 = time.perf_counter()
        sweep(rows, p, entry_grid=entry, hold_grid=hold, exit_grid=exits)
        sweep_s = time.perf_counter() - t0
        print(f"  sweep   {total} configurations in {sweep_s * 1000:.0f} ms "
              f"= {total / sweep_s:,.0f}/s, {sweep_s / total * 1e6:.0f} us each")
        print(f"  the same grid in python: {py_s * total:,.0f} s "
              f"({py_s / (sweep_s / total):,.0f}x)")
        return 0

    print(f"{version()}  ({'loaded' if available() else 'not built'}) "
          f"{LIB_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
