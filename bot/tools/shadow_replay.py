#!/usr/bin/env python3
"""Drive the shadow path over a recent window. Proves the wiring, not the rule.

WHAT THIS IS, AND THE ONE THING IT MUST NOT BECOME
==================================================
This replays the cleared rule through `ShadowStrategy` and the shadow caps over
a recent window, so that the caps, the monitors and the artefact schema can be
seen producing coherent output before anything runs unattended.

**It is not a measurement.** There is no rotation null behind it, no
drift-controlled contrast, no control validation and no percentile. Its artefact
is labelled `shadow_replay` and carries `is_stage1_evidence: false`, and it
cannot change `cleared_edge_signal` — the registration hook only accepts
`_summary.json` files that pass the slice-57 OOS gate, and this writes nothing
of the kind.

The specific temptation it exists next to: a replay that looks good invites
"see, it still works", and a replay that looks bad invites "let us adjust
FUND_ABS". Both are forbidden. §41h: *if replay looks weak, that is information
for humans — not a trigger to retune.* The constants are read from the frozen
module and this tool exposes no flag that could change one.

WHAT IT ACTUALLY EXERCISES
==========================
* the scope gate (`shadow_is_permitted`) — refuses if the flag is null or revoked
* the caps — one position, one entry per calendar day, 100 USD, BTCUSDT only
* the monitors — evaluated after every close, against the pre-declared thresholds
* the artefact schema the health payload reports

Because the caps allow at most one entry per day and one open position, the
replay's trade list is a SUBSET of what the Stage-1 measurement scored. That is
expected and is reported: a capped pilot is not the same object as an uncapped
backtest, and pretending otherwise would be the first step toward reading one as
the other.
"""
from __future__ import annotations

import argparse
import datetime as dt
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

# DATA-READ LEDGER (slice 78). Installed BEFORE the first load_corpus call so
# that every corpus this tool reads is recorded, and a future holdout can be
# certified untouched by tools/reserved_holdout.py. Reading is reading: a
# negative result steers the next hypothesis exactly as a positive one does,
# which is how slice 57 ended up "out of sample" on bytes slice 55 had read.
try:
    import reserved_holdout as _read_ledger
    _read_ledger.install()
except Exception as _ledger_exc:  # noqa: BLE001 - never blocks a measurement
    # NOT a silent pass: the reason is bound and the flag is legible. An
    # unrecorded read is a real loss (a future holdout cannot be certified),
    # but it must not take the measurement down with it.
    _read_ledger = None
    _READ_LEDGER_UNAVAILABLE = repr(_ledger_exc)




def iso(ms: int) -> str:
    return dt.datetime.fromtimestamp(
        ms / 1000.0, tz=dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir",
                        default=os.path.join(REPO, "data", "real_linear_1d"))
    parser.add_argument("--funding-data", default=os.path.join(
        REPO, "data", "real_funding", "funding",
        "BINANCE_LINEAR_BTC_USDT_FUNDING.csv.gz"))
    parser.add_argument("--warmup", type=int, default=200)
    parser.add_argument("--out", default=os.path.join(
        REPO, "artifacts", "slice58_shadow_replay.json"))
    args = parser.parse_args(argv)
    # NOTE: there is deliberately no flag here for FUND_ABS, the stop, the take
    # profit, the horizon or the costs. A replay tool that could change one is a
    # retuning tool with a monitoring label.

    status = ps.current()
    allowed, why = shadow.shadow_is_permitted(status)
    print("=" * 78)
    print("SLICE 58 — SHADOW REPLAY   (wiring proof, NOT Stage-1 evidence)")
    print("=" * 78)
    print(f"  cleared_edge_signal : {status.cleared_edge_signal!r}")
    print(f"  shadow permitted    : {allowed}  ({why})")
    if not allowed:
        print("\nREFUSING to replay: there is nothing in scope to shadow.")
        return 2

    folds = fb.load_folds()
    if not fb.folds_are_unmodified():
        print("REFUSING: the fold calendar's hash is not the locked one.")
        return 2

    loaded, _b, _n = md.load_corpus(args.data_dir, verify=False)
    bars = [bt.Bar(b.start_ms, b.open, b.high, b.low, b.close, b.volume)
            for b in loaded[shadow.SHADOW_SYMBOL]]
    funding = fb.load_funding(args.funding_data)

    flags, directions = fb.flags_and_directions(bars, funding,
                                                warmup=args.warmup)
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

    # Walk the late window bar by bar, applying the caps exactly as the live
    # shell would. This is the part that is being proved.
    caps = shadow.ShadowCaps()
    closed: list = []
    open_until = None
    considered = capped_out = 0

    # THE SCHEDULE MUST BE THE CLEARED RULE'S, NOT A COUSIN OF IT.
    #
    # The first version of this tool walked every flagged late bar and applied
    # the caps to each. That entered MID-RUN: 55 trades against the 41 the
    # cleared rule schedules, with a NEGATIVE mean net R where the measured
    # rule scored +0.1736. Rich funding persists for days, so "every flagged
    # bar" and "the first bar of each run" are not nearly the same object — and
    # a pilot monitoring the wrong one looks exactly like diligence.
    #
    # Candidates are therefore the entries `skill_test.simulate_schedule`
    # produces from the same tradable mask the Stage-1 run used, which is
    # one per contiguous run. The caps are then applied ON TOP.
    eligible = np.zeros(len(bars), dtype=bool)
    common = set(side_lookup[fb.LONG_SETUP]) & set(side_lookup[fb.SHORT_SETUP])
    eligible[sorted(common)] = True
    eligible[:args.warmup] = False
    late = np.zeros(len(bars), dtype=bool)
    late[list(fb.late_window_indices(bars, folds))] = True
    eligible = eligible & late
    tradable = em.tradable_flags(flags, eligible)
    candidates = [e for e in sk.simulate_schedule(
        tradable, warmup=args.warmup, lockup=fb.LOCKUP) if e in directions]

    for index in candidates:
        if open_until is not None and index >= open_until:
            caps.record_exit()
            open_until = None
        direction = directions.get(index)
        if direction is None or index not in side_lookup[direction]:
            continue
        considered += 1
        day = iso(bars[index].start_ms)[:10]
        blocked = caps.why_blocked(
            symbol=shadow.SHADOW_SYMBOL, day=day,
            notional_usd=shadow.SHADOW_MAX_NOTIONAL_USD)
        if blocked is not None:
            capped_out += 1
            continue
        position = side_lookup[direction][index]
        held = side_used[direction].get(index, fb.HORIZON)
        exit_index = min(index + 1 + held, len(bars) - 1)
        caps.record_entry(day=day)
        open_until = exit_index
        closed.append(shadow.ShadowTrade(
            entry_utc=iso(bars[min(index + 1, len(bars) - 1)].start_ms),
            exit_utc=iso(bars[exit_index].start_ms),
            direction=direction,
            net_r=float(side_net[direction][position]),
            notional_usd=shadow.SHADOW_MAX_NOTIONAL_USD))

    readings = shadow.evaluate_monitors(closed)
    mean_r = float(np.mean([t.net_r for t in closed])) if closed else None

    payload = {
        "schema": "shadow_replay/1",
        "slice": 58,
        "kind": "shadow_replay",
        "is_stage1_evidence": False,
        "registration_eligible": False,
        "why_not_evidence": (
            "No rotation null, no drift-controlled contrast, no control "
            "validation and no percentile. This replay exercises the caps, the "
            "monitors and the artefact schema. It cannot change "
            "cleared_edge_signal — the registration hook only accepts "
            "_summary.json artefacts that pass the slice-57 OOS gate, and this "
            "is not one."),
        "signal": shadow.SHADOW_SIGNAL,
        "symbol": shadow.SHADOW_SYMBOL,
        "constants_fingerprint": shadow.constants_fingerprint()[:32],
        "constants": dict(fb.CONSTANTS),
        "window": {"t_mid": folds["t_mid"], "t1": folds["t1"],
                   "bars": folds["n_bars_late"]},
        "caps": {
            "max_concurrent_positions": shadow.SHADOW_MAX_CONCURRENT_POSITIONS,
            "max_entries_per_day": shadow.SHADOW_MAX_ENTRIES_PER_DAY,
            "max_notional_usd": shadow.SHADOW_MAX_NOTIONAL_USD,
        },
        "setups_considered": considered,
        "entries_refused_by_caps": capped_out,
        "shadow_trades": len(closed),
        "shadow_mean_net_r": mean_r,
        "capping_note": (
            f"Candidates are the CLEARED RULE's own schedule — one entry per "
            f"contiguous run, from skill_test.simulate_schedule over the same "
            f"tradable mask the Stage-1 run used. {considered} candidates, "
            f"{capped_out} refused by the caps, {len(closed)} taken. A capped "
            f"pilot is still not the same object as an uncapped backtest and "
            f"the means are not strictly comparable, but the SCHEDULE is now "
            f"the same rule rather than a cousin of it. The first version of "
            f"this tool walked every flagged bar and entered mid-run, taking "
            f"55 trades with a negative mean where the rule it was shadowing "
            f"scored +0.1736 — see EDGE.md 41i."),
        "monitors": [
            {"name": r.name, "state": r.state, "value": r.value,
             "threshold": r.threshold, "n_trades": r.n_trades,
             "detail": r.detail}
            for r in readings
        ],
        "monitor_status": shadow.monitor_status(readings),
        "entries_blocked_by_monitor":
            shadow.entries_blocked_by_monitor(readings),
        "cleared_edge_signal_after_replay":
            ps.cleared_edge_signal_from_artifacts(
                os.path.join(REPO, "artifacts")),
        "live_authorized": False,
        "models_current_present": os.path.exists(
            os.path.join(REPO, "models", "current")),
        "trades": [
            {"entry_utc": t.entry_utc, "exit_utc": t.exit_utc,
             "direction": t.direction, "net_r": t.net_r}
            for t in closed
        ],
        "git_commit": _provenance.git_commit(REPO),
    }
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")

    print(f"  window              : {folds['t_mid']} .. {folds['t1']}")
    print(f"  setups considered   : {considered}")
    print(f"  refused by caps     : {capped_out}")
    print(f"  shadow trades       : {len(closed)}")
    print(f"  shadow mean net R   : "
          f"{mean_r:+.4f}" if mean_r is not None else "  n/a")
    print()
    for r in readings:
        value = "n/a" if r.value is None else f"{r.value:+.4f}"
        print(f"  {r.name:20s} {r.state:17s} {value:>9s}   {r.detail}")
    print()
    print(f"  monitor status      : {shadow.monitor_status(readings)}")
    print(f"  entries blocked     : "
          f"{shadow.entries_blocked_by_monitor(readings)}")
    print(f"  cleared_edge after  : "
          f"{payload['cleared_edge_signal_after_replay']!r}  (unchanged)")
    print()
    print("This is a WIRING PROOF. There is no null behind these numbers and")
    print("they are not evidence of skill. A weak replay is information for a")
    print("human; it is NOT a trigger to retune FUND_ABS, and this tool")
    print("exposes no flag that could.")
    print(f"\nartefact: {os.path.relpath(args.out, REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
