"""Schema-valid capacity/2 rows: one from the real captured traffic, then cloned into series."""
import copy
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import md_capacity as MC  # noqa: E402
from test_md_integration import complete_state  # noqa: E402

import random as _random

H8 = 8 * 3_600_000
START = 1_793_750_400_000          # 2026-11-04 00:00:00 UTC


def base_row():
    st, now, wall = complete_state()
    return MC.capacity_row(st.view(now, wall), now_ms=wall,
                           source={"store_record_id": 1, "perp_update_id": 5, "perp_seq": 7,
                                   "spot_update_id": 3, "spot_seq": 4},
                           health={"linear_reconnects": 0, "spot_reconnects": 0, "rest_errors": 0,
                                   "store_fault": None, "free_bytes": 4_000_000_000, "min_free_bytes": 536_870_912})


def clone(base, i, at_ms, forecast, next_ms, last_print=None, interval_h=8, env="mainnet", symbol="BTCUSDT"):
    r = copy.deepcopy(base)
    r["at_ms"] = at_ms
    r["environment"], r["symbol"] = env, symbol
    r["source"]["store_record_id"] = i
    r["row_id"] = f"{env}:{symbol}:{i}"
    r["times"]["perp_book_exch_ts_ms"] = at_ms - 40
    r["times"]["spot_book_exch_ts_ms"] = at_ms - 55
    r["funding"] = {"forecast_bps": forecast, "next_settlement_ms": next_ms, "interval_h": interval_h,
                    "last_print_bps": None if last_print is None else last_print[1],
                    "last_print_settled_ms": None if last_print is None else last_print[0]}
    r["funding_forecast_bps_per_settlement"] = forecast
    r["next_funding_ms"] = next_ms
    return r


def series(base, n_settlements, forecast_fn, realised_fn, cadence_ms=300_000, skip=None, start=START):
    """Rows at a fixed cadence across `n_settlements` funding intervals. The realised print for
    settlement T becomes visible from the first row at or after T. `skip(i)` drops rows."""
    rows = []
    t = start
    end = start + n_settlements * H8
    i = 0
    while t < end:
        k = t // H8
        T = (k + 1) * H8
        prev_T = k * H8
        i += 1
        if skip is None or not skip(i, t):
            lp = (prev_T, realised_fn(prev_T)) if t >= prev_T and prev_T >= start else None
            rows.append(clone(base, i, t, forecast_fn(T, t), T, lp))
        t += cadence_ms
    return rows


def noisy(mean, sd=0.2, seed=11, size=4000):
    """Realised funding with real variation and no persistence: what a claim-worthy series looks like.
    A constant series is NOT used where a claim is tested, because a constant has no variance to test."""
    rng = _random.Random(seed)
    vals = [mean + rng.gauss(0, sd) for _ in range(size)]
    return lambda T: vals[(T // H8) % size]
