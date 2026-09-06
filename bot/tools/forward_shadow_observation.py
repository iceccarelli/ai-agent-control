#!/usr/bin/env python3
"""Write the forward-COMPATIBLE shadow observation record. Not forward evidence.

THE DISTINCTION THIS TOOL EXISTS TO PROTECT
===========================================
A real forward shadow observes bars that did not exist when the rule was
measured. **There are none.** The linear corpus ends 2026-08-09; the cleared
measurement was taken over data ending on the same day. Zero bars have arrived
since.

So this writes the record a forward run will write, over the most recent window
available, using the same caps, the same schedule and the same monitor
thresholds — and it labels itself honestly:

    is_forward_observation        : false
    forward_observations_to_date  : 0
    is_stage1_evidence            : false
    registration_eligible         : false

The point is to fix the record's SHAPE before real observations start landing in
it, and to exercise the apparatus end to end. It is not to produce a number
anyone can cite.

**Relabelling history as experience is the exact substitution the promotion gate
exists to prevent**, so this tool is the one place in the repository where that
substitution would be easiest and most damaging. Hence the labels, hence the
tests that assert them, and hence the gate reading 0 / 20 rather than 35 / 20.

WHAT IT ENFORCES WHILE IT RUNS
==============================
* the scope gate — nothing is written if the flag is null, revoked, or the shell
  is not in paper;
* the CLEARED RULE'S SCHEDULE — one entry per contiguous run, taken from
  `skill_test.simulate_schedule` over the same tradable mask the Stage-1 run
  used. Slice 58 §41i is why this is not left to a bar-by-bar loop;
* the caps — 1 position, 1 entry per calendar day, 100.00 USD, BTCUSDT;
* the monitors — thresholds read from `shadow.MONITOR_THRESHOLDS`, never
  restated here, so a moved threshold cannot hide in this file.

There is deliberately **no flag** for a constant, a cap or a threshold. A tool
that could change one is a retuning tool with a monitoring label.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tools"))

import numpy as np  # noqa: E402

import backtest as bt  # noqa: E402
import edge_measurement as em  # noqa: E402
import market_data as md  # noqa: E402
import project_status as ps  # noqa: E402
import shadow  # noqa: E402
import skill_test as sk  # noqa: E402
from signals import funding_carry_fade_btc_v1 as fb  # noqa: E402
import provenance as _provenance  # noqa: E402

SCHEMA = "forward_shadow_observation/1"
SCHEDULE_MODE = "one_entry_per_contiguous_run"


def iso(ms: int) -> str:
    return dt.datetime.fromtimestamp(
        ms / 1000.0, tz=dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build(*, data_dir: str, funding_path: str, warmup: int,
          observed_at_utc: str) -> dict:
    status = ps.current()
    allowed, why = shadow.shadow_is_permitted(status)
    if not allowed:
        raise SystemExit(
            f"REFUSING to write an observation record: {why}")

    folds = fb.load_folds()
    if not fb.folds_are_unmodified():
        raise SystemExit(
            "REFUSING: the fold calendar's hash is not the locked one.")

    loaded, _b, _n = md.load_corpus(data_dir, verify=False)
    bars = [bt.Bar(b.start_ms, b.open, b.high, b.low, b.close, b.volume)
            for b in loaded[shadow.SHADOW_SYMBOL]]
    funding = fb.load_funding(funding_path)

    flags, directions = fb.flags_and_directions(bars, funding, warmup=warmup)
    flags = fb.restrict_flags_to_late(flags, bars, folds)

    side_lookup, side_net, side_used = {}, {}, {}
    for tag, side in ((fb.LONG_SETUP, "long"), (fb.SHORT_SETUP, "short")):
        idx, net, used = sk.barrier_r_for_all_bars(
            bars, take_profit_atr=fb.TAKE_PROFIT_ATR, stop_atr=fb.STOP_ATR,
            horizon=fb.HORIZON, atr_period=fb.ATR_PERIOD,
            round_trip_bps=fb.ROUND_TRIP_BPS, side=side, entry_on=fb.ENTRY_ON)
        side_net[tag] = fb.funding_adjusted_net_r(
            bars, funding, idx, net, used, tag,
            stop_atr=fb.STOP_ATR, atr_period=fb.ATR_PERIOD)
        side_lookup[tag] = {int(i): p for p, i in enumerate(idx)}
        side_used[tag] = {int(i): int(u) for i, u in zip(idx, used)}

    # THE CLEARED RULE'S SCHEDULE. One entry per contiguous run, from the same
    # builder the Stage-1 run used. Not a bar-by-bar loop — slice 58 §41i.
    eligible = np.zeros(len(bars), dtype=bool)
    eligible[sorted(set(side_lookup[fb.LONG_SETUP]) &
                    set(side_lookup[fb.SHORT_SETUP]))] = True
    eligible[:warmup] = False
    late = np.zeros(len(bars), dtype=bool)
    late[list(fb.late_window_indices(bars, folds))] = True
    eligible = eligible & late
    tradable = em.tradable_flags(flags, eligible)
    candidates = [e for e in sk.simulate_schedule(
        tradable, warmup=warmup, lockup=fb.LOCKUP) if e in directions]

    caps = shadow.ShadowCaps()
    closed, open_until, refused = [], None, 0
    for index in candidates:
        if open_until is not None and index >= open_until:
            caps.record_exit()
            open_until = None
        direction = directions[index]
        if index not in side_lookup[direction]:
            continue
        day = iso(bars[index].start_ms)[:10]
        if caps.why_blocked(symbol=shadow.SHADOW_SYMBOL, day=day,
                            notional_usd=shadow.SHADOW_MAX_NOTIONAL_USD):
            refused += 1
            continue
        held = side_used[direction].get(index, fb.HORIZON)
        exit_index = min(index + 1 + held, len(bars) - 1)
        caps.record_entry(day=day)
        open_until = exit_index
        closed.append(shadow.ShadowTrade(
            entry_utc=iso(bars[min(index + 1, len(bars) - 1)].start_ms),
            exit_utc=iso(bars[exit_index].start_ms),
            direction=direction,
            net_r=float(side_net[direction][side_lookup[direction][index]]),
            notional_usd=shadow.SHADOW_MAX_NOTIONAL_USD))

    readings = shadow.evaluate_monitors(closed)
    m4 = next((r for r in readings if r.name == "M4_halves"), None)
    corpus_last = iso(bars[-1].start_ms)

    return {
        "schema": SCHEMA,
        "slice": 59,
        "signal": shadow.SHADOW_SIGNAL,
        "symbol": shadow.SHADOW_SYMBOL,
        "observed_at_utc": observed_at_utc,

        # -- the honesty block, first, so it cannot be skimmed past ----------
        "is_forward_observation": False,
        "forward_observations_to_date": 0,
        "is_stage1_evidence": False,
        "registration_eligible": False,
        "why_this_is_not_forward_evidence": (
            f"The linear corpus ends {corpus_last[:10]}, which is the same data "
            f"the cleared measurement was taken over. ZERO bars have arrived "
            f"since. This record uses the schema, caps, schedule and monitor "
            f"thresholds a real forward run will use, over the most recent "
            f"available window, so the record's SHAPE is fixed before real "
            f"observations land in it. It is not experience. Relabelling "
            f"history as experience is the exact substitution the promotion "
            f"gate exists to prevent, which is why that gate counts 0 forward "
            f"observations and refuses."),
        "why_this_cannot_register": (
            "The registration hook reads only *_summary.json artefacts that "
            "pass the slice-57 OOS gate: registration_eligible true, window "
            "oos_late, a matching fold hash, >= 40 scored trades and a "
            "positive mean. This file is not a summary, declares "
            "registration_eligible false, and carries none of those fields."),

        # -- what was frozen, restated from the source of truth --------------
        "constants": dict(fb.CONSTANTS),
        "constants_fingerprint": shadow.constants_fingerprint()[:32],
        "constants_fingerprint_matches_slice58":
            shadow.constants_fingerprint()[:32] == shadow.CONSTANTS_FINGERPRINT,
        "schedule_mode": SCHEDULE_MODE,
        "schedule_note": (
            "One entry per contiguous setup run, from "
            "skill_test.simulate_schedule over the same tradable mask the "
            "Stage-1 run used. NOT every flagged bar: slice 58 §41i records "
            "that the first shadow wiring did exactly that and took 55 trades "
            "at -0.0464 mean net R where the cleared rule schedules 41 at "
            "+0.1736 — a pilot monitoring a rule that loses money while "
            "reporting healthy status."),
        "caps": {
            "max_concurrent_positions": shadow.SHADOW_MAX_CONCURRENT_POSITIONS,
            "max_entries_per_day": shadow.SHADOW_MAX_ENTRIES_PER_DAY,
            "max_notional_usd": shadow.SHADOW_MAX_NOTIONAL_USD,
            "symbol": shadow.SHADOW_SYMBOL,
        },

        # -- the window ------------------------------------------------------
        "window": {
            "source": "artifacts/funding_carry_fade_btc_v1_folds.json",
            "folds_sha256": fb.folds_sha256(),
            "t_mid": folds["t_mid"],
            "t1": folds["t1"],
            "bars": folds["n_bars_late"],
            "corpus_last_bar_utc": corpus_last,
            "bars_after_the_cleared_measurement": 0,
            "invented_future_bars": 0,
        },

        # -- what the pilot did ---------------------------------------------
        "setups_or_candidates": len(candidates),
        "entries_taken": len(closed),
        "entries_refused_by_caps": refused,
        "shadow_n_trades": len(closed),
        "shadow_mean_net_r": (float(np.mean([t.net_r for t in closed]))
                              if closed else None),
        "shadow_mean_note": (
            "A capped pilot's mean, with no rotation null, no "
            "drift-controlled contrast and no control behind it. It is not "
            "comparable with the Stage-1 mean and it is not evidence."),

        # -- the decay log ---------------------------------------------------
        "monitor_thresholds": shadow.MONITOR_THRESHOLDS,
        "monitor_readings": [
            {"name": r.name, "state": r.state, "value": r.value,
             "threshold": r.threshold, "n_trades": r.n_trades,
             "detail": r.detail}
            for r in readings
        ],
        "monitor_status": shadow.monitor_status(readings),
        "entries_blocked_by_monitor":
            shadow.entries_blocked_by_monitor(readings),
        "m4_recent_half_note": (
            f"M4_halves reads {m4.state} — {m4.detail}. This is EXPECTED "
            f"history, recorded in EDGE.md §41j and §42d and carried into the "
            f"promotion gate as an item a human must explicitly accept. It is "
            f"NOT a defect to erase: the only legitimate way it stops warning "
            f"is new forward data recovering under the threshold AS IT STANDS."
            if m4 is not None else "M4_halves produced no reading."),
        "thresholds_moved_this_slice": False,

        # -- the standing posture -------------------------------------------
        "cleared_edge_signal": status.cleared_edge_signal,
        "cleared_edge_re_scored_this_slice": False,
        "live_authorized": bool(status.live_authorized),
        "policy_mode": status.policy_mode,
        "execution_mode": status.execution_mode,
        "models_current_present": bool(status.models_current_present),
        "frozen_absent_count": len(ps.ABSENT_SIGNALS),
        "closer_to_autonomous_profit_agent": False,

        "trades": [
            {"entry_utc": t.entry_utc, "exit_utc": t.exit_utc,
             "direction": t.direction, "net_r": t.net_r,
             "notional_usd": t.notional_usd}
            for t in closed
        ],
        "git_commit": _provenance.git_commit(REPO),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir",
                        default=os.path.join(REPO, "data", "real_linear_1d"))
    parser.add_argument("--funding-data", default=os.path.join(
        REPO, "data", "real_funding", "funding",
        "BINANCE_LINEAR_BTC_USDT_FUNDING.csv.gz"))
    parser.add_argument("--warmup", type=int, default=200)
    parser.add_argument("--observed-at-utc", required=True,
                        help="ISO UTC stamp, passed in so the tool is pure")
    parser.add_argument("--out", default=os.path.join(
        REPO, "artifacts", "slice59_forward_shadow.json"))
    parser.add_argument("--log", default=os.path.join(
        REPO, "artifacts", "slice59_forward_shadow.log"))
    args = parser.parse_args(argv)
    # No flag for a constant, a cap or a threshold. Deliberately.

    payload = build(data_dir=args.data_dir, funding_path=args.funding_data,
                    warmup=args.warmup, observed_at_utc=args.observed_at_utc)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")

    lines = [
        "=" * 78,
        "SLICE 59 — FORWARD-COMPATIBLE SHADOW OBSERVATION",
        "=" * 78,
        "",
        "  THIS IS NOT A FORWARD OBSERVATION.",
        f"  is_forward_observation       : {payload['is_forward_observation']}",
        f"  forward_observations_to_date : "
        f"{payload['forward_observations_to_date']}",
        f"  corpus last bar              : "
        f"{payload['window']['corpus_last_bar_utc'][:10]}",
        f"  bars since the cleared run   : "
        f"{payload['window']['bars_after_the_cleared_measurement']}",
        "",
        "  The record uses the schema, caps, schedule and thresholds a real",
        "  forward run will use, so its shape is fixed before real",
        "  observations land in it. It is not experience, and the promotion",
        "  gate counts it as zero.",
        "",
        "-" * 78,
        f"  signal            : {payload['signal']} ({payload['symbol']})",
        f"  constants f'print : {payload['constants_fingerprint']}  "
        f"(matches slice 58: "
        f"{payload['constants_fingerprint_matches_slice58']})",
        f"  schedule mode     : {payload['schedule_mode']}",
        f"  caps              : "
        f"{payload['caps']['max_concurrent_positions']} position, "
        f"{payload['caps']['max_entries_per_day']} entry/day, "
        f"{payload['caps']['max_notional_usd']:.2f} USD",
        f"  window            : {payload['window']['t_mid'][:10]} .. "
        f"{payload['window']['t1'][:10]}  "
        f"({payload['window']['bars']} bars)",
        "",
        f"  candidates        : {payload['setups_or_candidates']}",
        f"  refused by caps   : {payload['entries_refused_by_caps']}",
        f"  entries taken     : {payload['entries_taken']}",
        f"  shadow mean net R : {payload['shadow_mean_net_r']:+.4f}"
        if payload["shadow_mean_net_r"] is not None else
        "  shadow mean net R : n/a",
        "",
        "-" * 78,
        "  DECAY LOG — thresholds unchanged from slice 58",
        "-" * 78,
    ]
    for reading in payload["monitor_readings"]:
        value = ("n/a" if reading["value"] is None
                 else f"{reading['value']:+.4f}")
        lines.append(f"  {reading['name']:20s} {reading['state']:17s} "
                     f"{value:>9s}   {reading['detail']}")
    lines += [
        "",
        f"  monitor status    : {payload['monitor_status']}",
        f"  entries blocked   : {payload['entries_blocked_by_monitor']}",
        f"  thresholds moved  : {payload['thresholds_moved_this_slice']}",
        "",
        f"  {payload['m4_recent_half_note']}",
        "",
        "-" * 78,
        f"  cleared_edge_signal    : {payload['cleared_edge_signal']}  "
        f"(re-scored this slice: "
        f"{payload['cleared_edge_re_scored_this_slice']})",
        f"  live_authorized        : {payload['live_authorized']}",
        f"  policy_mode            : {payload['policy_mode']}",
        f"  models_current_present : {payload['models_current_present']}",
        f"  frozen families        : {payload['frozen_absent_count']}",
        f"  closer to autonomy     : "
        f"{payload['closer_to_autonomous_profit_agent']}",
        "",
        "  is_stage1_evidence     : False",
        "  registration_eligible  : False",
        "",
        f"artefact: {os.path.relpath(args.out, REPO)}",
        "",
    ]
    text = "\n".join(lines)
    with open(args.log, "w", encoding="utf-8") as handle:
        handle.write(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
