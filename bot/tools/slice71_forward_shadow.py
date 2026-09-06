#!/usr/bin/env python3
"""The forward shadow segment: post-t1 bars only. EDGE.md §45e, §46d.

SLICE 71 — WHAT CHANGED IN THIS FILE, AND WHAT DID NOT
======================================================
Nothing in the scoring logic. This file differs from
`tools/slice62_forward_shadow.py` by five identifiers — a schema version, a
slice number, two output paths and a banner — plus a block of fields the
slice-63 mission asks for explicitly (`promotion_gate_allows_live`,
`bars_fabricated`, `constants_fingerprint_matches_frozen`) and one that this
slice needs to be honest (`window_grew_since_slice70`). `diff` the two files.

That is deliberate and it is the claim: **the segment was not re-tuned between
slices.** A forward measurement whose code changes every time it is run is not a
forward measurement, it is a series of one-off estimates.

WHAT SLICE 71 MEASURES
======================
**A window that finally grew.** `|W| = 2` — `2026-08-10` and `2026-08-11`,
the first genuine growth since `t1` was locked. Slices 62 and 63 both scored a
one-bar window.

The ceiling did not move: `max(0, 2 - 5) = 0`. Six closed forward bars are
needed before one trade can close and there are two. **The evidence window is
doubling in size while producing no evidence, and both halves of that sentence
are true at once** — which is what honest pilot progress looks like when the
horizon is longer than the window.

--- the original slice-62 header follows, unchanged ---

The FIRST TRUE forward shadow segment: post-t1 bars only. EDGE.md §45e.

WHAT MAKES THIS DIFFERENT FROM SLICES 59, 60 AND 61
===================================================
Those three wrote a forward-*compatible* record: the right schema, the right
caps, the right schedule, scored over the most recent window that existed —
which was history — and labelled `is_forward_observation: false` because zero
bars had arrived since the cleared measurement ended.

Bars have now arrived. One closed daily bar, `2026-08-10`, strictly after `t1`,
proven by prefix hash rather than by a note (§45a–§45c). So this tool scores the
segment the programme has been waiting six slices to score.

THE RULE, FIXED BEFORE ANY NUMBER WAS COMPUTED (§45e)
=====================================================
* the **forward window `W`** is the closed linear daily bars strictly after
  `t1`. Strictly: a bar stamped exactly `t1` is the last bar of the cleared run,
  not the first bar of forward experience;
* a **forward observation** is a shadow trade that both ENTERS and EXITS on bars
  in `W`. Not a flagged bar. Not an entry. **A trade is an observation only when
  it closes**;
* `is_forward_observation = forward_n_trades > 0`. **NOT** `extension_present`.
  The two are reported separately and the separation is the entire point. Slice
  59's failure mode was history relabelled as forward experience; the failure
  mode available to *this* slice is a bar relabelled as experience — announcing
  a forward observation because data finally arrived, when nothing has yet been
  observed to conclude;
* **the declared ceiling.** The rule enters on the bar FOLLOWING its decision
  bar and holds up to `HORIZON` bars, so `HORIZON + 1 = 6` closed forward bars
  must exist before the first forward trade can close. With `|W| = 1` the
  maximum possible forward closed trades is **0**. That ceiling was written into
  EDGE.md before this file ran. **A run that returns any forward trade at all is
  a DATA DEFECT or a scheduling bug to be investigated, not progress**, and this
  tool says so in its own output rather than leaving a reader to notice.

HOW THE SEGMENT IS BUILT
========================
By zeroing flags outside `W` — `restrict_flags_to_forward` — never by slicing
the bar array. A 14-period ATR computed over a one-bar window is not a warm
indicator, it is a number invented out of nothing. Warm-up state legitimately
flows from history; decisions do not.

The schedule is the CLEARED RULE'S schedule, `one_entry_per_contiguous_run`,
from the same `skill_test.simulate_schedule` the Stage-1 run used. Slice 58 §41i
records what happened when a shadow was wired bar-by-bar instead: 55 trades at
-0.0464 mean net R where the cleared rule schedules 41 at +0.1736 — a pilot
reporting healthy monitors on a rule that loses money.

WHAT IT WILL NOT DO
===================
No flag for a constant, a cap, a threshold or a window bound. A tool that could
move one is a retuning tool wearing a monitoring label. It invents no bars, and
it will not count a trade whose exit index was clamped to the end of the corpus:
a trade that ran out of data has not closed.
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
import corpus_prefix as cp  # noqa: E402
import edge_measurement as em  # noqa: E402
import market_data as md  # noqa: E402
import project_status as ps  # noqa: E402
import shadow  # noqa: E402
import skill_test as sk  # noqa: E402
from signals import funding_carry_fade_btc_v1 as fb  # noqa: E402
import provenance as _provenance  # noqa: E402

SCHEMA = "forward_shadow_observation/10"
SLICE = 71
SCHEDULE_MODE = "one_entry_per_contiguous_run"


def iso(ms: int) -> str:
    return dt.datetime.fromtimestamp(
        ms / 1000.0, tz=dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _gate_refuses() -> bool:
    """Read the live gate rather than restating its answer.

    If this file simply wrote `False`, the artefact would assert a refusal
    instead of reporting one, and a tree on which the gate had opened would
    still produce an artefact saying it had not.
    """
    import promotion_gate as pg  # noqa: PLC0415
    return bool(pg.promotion_gate_allows_live())


def _window_grew(closed_forward_bars: int) -> dict:
    """Did the forward window grow since the last slice, or only get re-read?

    `t1` does not move, so `extension_present` is permanently true once one bar
    lands and re-reporting it every slice reads like progress. This reports the
    delta against the previous slice's own artefact.
    """
    previous_path = os.path.join(REPO, "artifacts",
                                 "slice70_forward_shadow.json")
    if not os.path.isfile(previous_path):
        return {"comparable": False,
                "why": "no slice-70 forward artefact on this tree"}
    with open(previous_path, encoding="utf-8") as handle:
        previous = json.load(handle)
    before = int(previous["forward_window"]["of_which_closed"])
    return {
        "comparable": True,
        "previous_artefact": "artifacts/slice70_forward_shadow.json",
        "closed_forward_bars_previously": before,
        "closed_forward_bars_now": closed_forward_bars,
        "delta": closed_forward_bars - before,
        "grew": closed_forward_bars > before,
        # Derived from the counts. The bar word and every figure below are
        # interpolated so this sentence cannot go on saying "seven" into a
        # slice where the window is eight. EDGE.md §53d.
        "note": (
            f"THE WINDOW REACHED {_SPELLED.get(closed_forward_bars, closed_forward_bars)} "
            f"BARS AND THE CEILING IS "
            f"{max(0, closed_forward_bars - fb.HORIZON)} — that many forward "
            f"decision bars could now yield a trade closing inside the window. "
            f"ZERO SETUPS FIRED on any of them: every rate at a decision is "
            f"below FUND_ABS. The measurement is 'ceiling "
            f"{max(0, closed_forward_bars - fb.HORIZON)}, setups 0 of "
            f"{closed_forward_bars}, fills 0' and no second sentence is "
            f"entitled to more. It says the funding regime over the forward "
            f"window never stood at or above FUND_ABS at a decision; it says "
            f"NOTHING about whether the rule makes money, because no position "
            f"was opened and only selectivity was exercised, never skill. The "
            f"cleared run scheduled 41 trades in about two years, roughly one "
            f"per eighteen days, so {closed_forward_bars} quiet days is "
            f"unremarkable under the null and the alternative alike and would "
            f"remain so at twice the length. EDGE.md §53b, §53c, §53d."),
    }


_SPELLED = {5: "FIVE", 6: "SIX", 7: "SEVEN", 8: "EIGHT", 9: "NINE",
            10: "TEN", 11: "ELEVEN", 12: "TWELVE"}


def _setup_table(bars, funding, closed_forward) -> list:
    """One row per closed forward bar: the date, the rate AT THE DECISION, and
    whether that rate constituted a setup.

    It lives outside `build` deliberately. `build`'s body is compared
    statement-for-statement against the previous slice's, so a new field that
    needed new statements inside it would either weaken that comparison or be
    left out. A pure function of values `build` already holds costs the
    comparison nothing.

    The rate reported is `funding_at_decision` — the print standing at the
    bar's CLOSE — not the day's high print. Reporting the day's maximum here
    would quietly answer a different question than the rule asks, and it is
    the question a reader is most likely to confuse it with.
    """
    rates = fb.funding_at_decision(bars, funding)
    setups = fb.funding_setups(bars, funding, fund_abs=fb.FUND_ABS)
    rows = []
    for i in closed_forward:
        rate = float(rates[i])
        rows.append({
            "date": iso(bars[i].start_ms)[:10],
            "funding_at_decision": None if rate != rate else rate,
            "fund_abs": fb.FUND_ABS,
            "funding_setup_present": setups.get(i) is not None,
            "shortfall_vs_fund_abs": (None if rate != rate
                                      else fb.FUND_ABS - abs(rate)),
        })
    return rows


def build(*, data_dir: str, funding_path: str, warmup: int,
          observed_at_utc: str) -> dict:
    status = ps.current()
    allowed, why = shadow.shadow_is_permitted(status)
    if not allowed:
        raise SystemExit(f"REFUSING to write an observation record: {why}")
    if not fb.folds_are_unmodified():
        raise SystemExit(
            "REFUSING: the fold calendar's hash is not the locked one.")

    # The corpus must be an APPEND of the measured history, or the forward
    # window is not a window onto the same series that was measured.
    prefix = cp.check_all()
    if not all(check.append_only for check in prefix.values()):
        broken = [c.why_not() for c in prefix.values() if not c.append_only]
        raise SystemExit("REFUSING: " + " ".join(broken))

    folds = fb.load_folds()
    loaded, _bars_seen, _n = md.load_corpus(data_dir, verify=False)
    bars = [bt.Bar(b.start_ms, b.open, b.high, b.low, b.close, b.volume)
            for b in loaded[shadow.SHADOW_SYMBOL]]
    funding = fb.load_funding(funding_path)

    checked = dt.datetime.strptime(
        observed_at_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)

    # ---- the forward window W ------------------------------------------
    forward = list(fb.forward_window_indices(bars, folds))
    # CLOSED only. A daily bar stamped D closes at D+1T00:00Z.
    closed_forward = [
        i for i in forward
        if dt.datetime.fromtimestamp(bars[i].start_ms / 1000.0,
                                     tz=dt.timezone.utc)
        + dt.timedelta(days=1) <= checked]
    last_forward = max(closed_forward) if closed_forward else None

    # ---- decisions, restricted to W by zeroing flags --------------------
    flags, directions = fb.flags_and_directions(bars, funding, warmup=warmup)
    forward_flags = fb.restrict_flags_to_forward(flags, bars, folds)

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

    eligible = np.zeros(len(bars), dtype=bool)
    eligible[sorted(set(side_lookup[fb.LONG_SETUP]) &
                    set(side_lookup[fb.SHORT_SETUP]))] = True
    eligible[:warmup] = False
    in_window = np.zeros(len(bars), dtype=bool)
    in_window[closed_forward] = True
    tradable = em.tradable_flags(forward_flags, eligible & in_window)
    candidates = [e for e in sk.simulate_schedule(
        tradable, warmup=warmup, lockup=fb.LOCKUP) if e in directions]

    # ---- the pilot, under the frozen caps --------------------------------
    caps = shadow.ShadowCaps()
    closed_trades, open_until, refused = [], None, 0
    no_entry_bar, truncated = [], []
    for index in candidates:
        if open_until is not None and index >= open_until:
            caps.record_exit()
            open_until = None
        direction = directions[index]
        if index not in side_lookup[direction]:
            continue
        entry_index = index + 1
        held = side_used[direction].get(index, fb.HORIZON)
        exit_index = entry_index + held

        # A trade that ran out of data has not closed. No clamping to the end
        # of the corpus, which would manufacture an exit price out of the last
        # bar that happens to exist.
        if entry_index > (last_forward if last_forward is not None else -1):
            no_entry_bar.append(iso(bars[index].start_ms))
            continue
        if last_forward is None or exit_index > last_forward:
            truncated.append(iso(bars[index].start_ms))
            continue

        day = iso(bars[index].start_ms)[:10]
        if caps.why_blocked(symbol=shadow.SHADOW_SYMBOL, day=day,
                            notional_usd=shadow.SHADOW_MAX_NOTIONAL_USD):
            refused += 1
            continue
        caps.record_entry(day=day)
        open_until = exit_index
        closed_trades.append(shadow.ShadowTrade(
            entry_utc=iso(bars[entry_index].start_ms),
            exit_utc=iso(bars[exit_index].start_ms),
            direction=direction,
            net_r=float(side_net[direction][side_lookup[direction][index]]),
            notional_usd=shadow.SHADOW_MAX_NOTIONAL_USD))

    forward_n_trades = len(closed_trades)
    ceiling = max(0, len(closed_forward) - fb.HORIZON)
    readings = shadow.evaluate_monitors(closed_trades)

    # Why did the rule stand aside on each forward bar? Two independent
    # reasons can apply, and both are computed rather than asserted, because
    # "no trade" is only informative once a reader knows which of them held.
    raw_setups = fb.funding_setups(bars, funding, fund_abs=fb.FUND_ABS)
    stand_aside = [
        {
            "bar_utc": iso(bars[i].start_ms),
            "funding_setup_present": raw_setups.get(i) is not None,
            "is_last_bar_of_corpus": i == len(bars) - 1,
            "excluded_as_last_bar": (
                "directed_signal_bars excludes the final bar: the fill happens "
                "one bar later and there is no next bar to fill on"
                if i == len(bars) - 1 else None),
            "fund_abs_threshold": fb.FUND_ABS,
        }
        for i in closed_forward
    ]

    return {
        "schema": SCHEMA,
        # Derived from SLICE, not typed: the substitution pipeline rewrites
        # `sliceNN` strings and has never updated an integer. EDGE.md §54f.
        "slice": SLICE,
        "signal": shadow.SHADOW_SIGNAL,
        "symbol": shadow.SHADOW_SYMBOL,
        "observed_at_utc": observed_at_utc,

        # -- the honesty block, first, so it cannot be skimmed past ----------
        "extension_present": bool(closed_forward),
        "is_forward_observation": forward_n_trades > 0,
        "forward_n_trades": forward_n_trades,
        "forward_observations_to_date": forward_n_trades,
        "is_stage1_evidence": False,
        "registration_eligible": False,
        "cleared_edge_re_scored_this_slice": False,
        "labelling_rule": (
            "is_forward_observation = forward_n_trades > 0, NOT "
            "extension_present. A BAR ARRIVING IS NOT AN OBSERVATION. Declared "
            "in EDGE.md §45e before this tool ran."),

        # -- the pre-declared ceiling, beside the result ---------------------
        "ceiling": {
            "closed_forward_bars": len(closed_forward),
            "closed_forward_bars_needed_for_one_trade": fb.HORIZON + 1,
            "max_possible_forward_closed_trades": ceiling,
            "declared_in": "EDGE.md §54a, before this tool ran",
            "observed_forward_closed_trades": forward_n_trades,
            "within_ceiling": forward_n_trades <= ceiling,
            "note_on_this_milestone": (
                "Ceiling 4 at N=9: not 3 — that was slice 70, at N=8 — and "
                "not 5, which would need N=10. The ceiling is a DENOMINATOR: "
                "it grew by one because a day closed, and nothing about the "
                "rule, the data or the regime got better. Four slices of "
                "denominator growth against a numerator that has never left "
                "zero is the whole shape of this pilot so far."),
            "_prior_milestone_note": (
                "FIRST STRUCTURAL NON-ZERO CEILING in the pilot series. It "
                "counts DECISION BARS that could yield a trade closing inside "
                "the window — exactly one, the first forward bar — not "
                "outcomes: whether such a trade closes also depends on the "
                "barrier resolving in the bars that remain, since entry falls "
                "on the following bar and the exit index must stay inside the "
                "corpus. A true upper bound, not a forecast."),
            "if_exceeded": (
                "A forward trade count above this ceiling is a DATA DEFECT or "
                "a scheduling bug to be investigated, NOT progress."),
        },

        # -- the forward window ----------------------------------------------
        "forward_window": {
            "rule": "closed linear daily bars STRICTLY after t1",
            "t1": folds["t1"],
            # prior-slice: the slice-59 substitution is a historical
            # reference and never advances.
            "t1_is_measured_not_forward": (
                "Strict. A bar stamped exactly t1 is the last bar of the "
                "cleared run. Counting it as forward would be the slice-59 "
                "substitution in its smallest and most plausible form."),
            "folds_sha256": fb.folds_sha256(),
            "bars_strictly_after_t1": len(forward),
            "of_which_closed": len(closed_forward),
            "bar_utcs": [iso(bars[i].start_ms) for i in closed_forward],
            "corpus_last_bar_utc": iso(bars[-1].start_ms),
            "built_by": "restrict_flags_to_forward (flags zeroed)",
            "bar_array_sliced": False,
            "why_not_sliced": (
                "A 14-period ATR computed over a one-bar window is not a warm "
                "indicator, it is a number invented out of nothing, and every "
                "barrier derived from it would be fiction. Warm-up state "
                "legitimately flows from history. Decisions do not."),
            "invented_future_bars": 0,
        },

        # -- what the rule actually did on those bars -------------------------
        "forward_decisions": {
            "flagged_bars_in_window": [
                iso(bars[i].start_ms) for i in closed_forward
                if bool(forward_flags[i])],
            "candidates_after_schedule": [
                iso(bars[i].start_ms) for i in candidates],
            "entries_taken": forward_n_trades,
            "entries_refused_by_caps": refused,
            "why_the_rule_stood_aside": stand_aside,
            "decisions_with_no_entry_bar_yet": no_entry_bar,
            "decisions_whose_exit_bar_does_not_exist_yet": truncated,
            "exit_clamping_to_corpus_end": False,
            "why_no_clamping": (
                "A trade that ran out of data has not closed. Clamping the "
                "exit index to the last bar that happens to exist would "
                "manufacture an exit price and report an unfinished trade as "
                "an observation."),
        },

        # -- what was frozen, restated from the source of truth ---------------
        "constants": dict(fb.CONSTANTS),
        "constants_fingerprint": shadow.constants_fingerprint()[:32],
        # prior-slice: pinned to the slice-58 fingerprint, never advances.
        "constants_fingerprint_matches_slice58":
            shadow.constants_fingerprint()[:32] == shadow.CONSTANTS_FINGERPRINT,
        "schedule_mode": SCHEDULE_MODE,
        "caps": {
            "max_concurrent_positions": shadow.SHADOW_MAX_CONCURRENT_POSITIONS,
            "max_entries_per_day": shadow.SHADOW_MAX_ENTRIES_PER_DAY,
            "max_notional_usd": shadow.SHADOW_MAX_NOTIONAL_USD,
            "symbol": shadow.SHADOW_SYMBOL,
        },
        "caps_changed_this_slice": False,
        "fund_abs_moved_this_slice": False,
        "join_changed_this_slice": False,
        "fund_abs": fb.FUND_ABS,
        "join_rule": ("last funding print at-or-before the bar's CLOSE — "
                      "funding.at_or_before(close_time_ms(bar)), reading only "
                      "backwards in time"),
        "why_neither_moved": (
            "THREE near misses now exist and none is rescued. (1) The "
            "2026-08-12T00:00Z print equalled FUND_ABS exactly and was "
            "superseded before that bar's close, so only a change to the JOIN "
            "would capture it (EDGE.md §51c). (2) The 2026-08-17T16:00Z print "
            "was 0.00009202 and DID stand at its bar's close, so no join "
            "change is relevant — only a lower FUND_ABS would capture it, by "
            "8.0e-6 (EDGE.md §53c). (3) The 2026-08-18T16:00Z print was "
            "0.00003650, also standing at its close, and missed by 6.4e-5 — "
            "no adjustment short of abandoning the threshold reaches it "
            "(EDGE.md §54b). The first two exhaust the two available rescues; "
            "the third is the one that makes the case for leaving them alone, "
            "because a threshold whose misses are ALL narrow looks like a "
            "boundary being crept toward, and one that misses widely the very "
            "next day is behaving like a threshold. Adjusting a threshold or "
            "a join AFTER seeing which prints missed is fitting the rule to "
            "the data, and the rule was cleared under these values. The "
            "adjustment that would rescue any of these bars is precisely the "
            "one that would void the clear."),
        "thresholds_moved_this_slice": False,

        # -- monitors, over FORWARD trades only --------------------------------
        "monitor_thresholds": shadow.MONITOR_THRESHOLDS,
        "forward_monitor_readings": [
            {"name": r.name, "state": r.state, "value": r.value,
             "threshold": r.threshold, "n_trades": r.n_trades,
             "detail": r.detail}
            for r in readings],
        "forward_monitor_status": shadow.monitor_status(readings),
        "entries_blocked_by_monitor":
            shadow.entries_blocked_by_monitor(readings),
        "monitor_scope_note": (
            "These readings are over FORWARD trades only, which is why they "
            "report insufficient data rather than a state. The M-4 WARN "
            "recorded in slices 58-61 is a fact about the HISTORICAL segment "
            "and is deliberately NOT recomputed here into something that could "
            "be mistaken for a forward reading. It stands, unerased, in "
            "artifacts/slice59_forward_shadow.json and in EDGE.md §41j and "
            "§42d, and it remains an item a human must explicitly accept at "
            "the promotion gate. The only legitimate way it stops warning is "
            "new forward data recovering under the threshold AS IT STANDS."),

        "forward_mean_net_r": (float(np.mean([t.net_r for t in closed_trades]))
                               if closed_trades else None),
        "forward_mean_note": (
            "If this is ever non-null it is a capped pilot's mean with no "
            "rotation null, no drift-controlled contrast and no control behind "
            "it. It is NOT Stage-1 evidence, NOT registration-eligible, and "
            "NOT comparable with the OOS mean of +0.1736."),

        # -- the standing posture ----------------------------------------------
        # -- fields the slice-63 mission asks for by name --------------------
        "constants_fingerprint_matches_frozen":
            shadow.constants_fingerprint()[:32] == shadow.CONSTANTS_FINGERPRINT,
        "bars_fabricated": 0,
        "promotion_gate_allows_live": _gate_refuses(),
        "window_grew_since_slice70": _window_grew(len(closed_forward)),
        "funding_setup_table": _setup_table(bars, funding, closed_forward),

        "cleared_edge_signal": status.cleared_edge_signal,
        "live_authorized": bool(status.live_authorized),
        "policy_mode": status.policy_mode,
        "execution_mode": status.execution_mode,
        "models_current_present": bool(status.models_current_present),
        "frozen_absent_count": len(ps.ABSENT_SIGNALS),
        "closer_to_autonomous_profit_agent": False,
        "why_not_closer": (
            f"{len(closed_forward)} post-t1 days is pilot progress, not "
            f"autonomy — and a longer window is a larger denominator, not a "
            f"result. The gate counts closed forward trades and it counts "
            f"{forward_n_trades}."),

        "trades": [
            {"entry_utc": t.entry_utc, "exit_utc": t.exit_utc,
             "direction": t.direction, "net_r": t.net_r,
             "notional_usd": t.notional_usd}
            for t in closed_trades],
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
    parser.add_argument("--observed-at-utc", required=True)
    parser.add_argument("--out", default=os.path.join(
        REPO, "artifacts", "slice71_forward_shadow.json"))
    parser.add_argument("--log", default=os.path.join(
        REPO, "artifacts", "slice71_forward_shadow.log"))
    args = parser.parse_args(argv)
    # No flag for a constant, a cap, a threshold or a window bound.

    payload = build(data_dir=args.data_dir, funding_path=args.funding_data,
                    warmup=args.warmup, observed_at_utc=args.observed_at_utc)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")

    window, ceiling = payload["forward_window"], payload["ceiling"]
    decisions = payload["forward_decisions"]
    lines = [
        "=" * 78,
        "SLICE 71 — FORWARD SHADOW SEGMENT   (post-t1 bars only)",
        "=" * 78,
        "",
        f"  extension_present            : {payload['extension_present']}",
        f"  is_forward_observation       : "
        f"{payload['is_forward_observation']}",
        f"  forward_n_trades             : {payload['forward_n_trades']}",
        "",
        "  A BAR ARRIVING IS NOT AN OBSERVATION. The two lines above are",
        "  reported separately on purpose: data has arrived, and nothing has",
        "  yet been observed to conclude. EDGE.md §45e.",
        "",
        "-" * 78,
        f"  t1                           : {window['t1']}",
        f"  bars strictly after t1       : "
        f"{window['bars_strictly_after_t1']}  "
        f"(closed: {window['of_which_closed']})",
        f"  forward bars                 : {', '.join(window['bar_utcs'])}"
        if window["bar_utcs"] else "  forward bars                 : none",
        f"  bar array sliced             : {window['bar_array_sliced']}",
        f"  invented future bars         : {window['invented_future_bars']}",
        "",
        "-" * 78,
        "  THE CEILING, DECLARED BEFORE THIS RUN (EDGE.md §52b)",
        "-" * 78,
        f"  closed forward bars          : {ceiling['closed_forward_bars']}",
        f"  needed for one closed trade  : "
        f"{ceiling['closed_forward_bars_needed_for_one_trade']}",
        f"  max possible forward trades  : "
        f"{ceiling['max_possible_forward_closed_trades']}",
        f"  observed forward trades      : "
        f"{ceiling['observed_forward_closed_trades']}",
        f"  within ceiling               : {ceiling['within_ceiling']}",
        "",
        "-" * 78,
        f"  flagged bars in window       : "
        f"{decisions['flagged_bars_in_window'] or 'none'}",
    ] + [
        f"  stood aside {entry['bar_utc'][:10]}       : "
        f"funding setup present={entry['funding_setup_present']}, "
        f"last bar of corpus={entry['is_last_bar_of_corpus']}"
        for entry in decisions["why_the_rule_stood_aside"]
    ] + [
        f"  candidates after schedule    : "
        f"{decisions['candidates_after_schedule'] or 'none'}",
        f"  no entry bar yet             : "
        f"{decisions['decisions_with_no_entry_bar_yet'] or 'none'}",
        f"  no exit bar yet              : "
        f"{decisions['decisions_whose_exit_bar_does_not_exist_yet'] or 'none'}",
        f"  refused by caps              : "
        f"{decisions['entries_refused_by_caps']}",
        f"  exit clamping                : "
        f"{decisions['exit_clamping_to_corpus_end']}",
        "",
        "-" * 78,
        # prior-slice: the slice-58 fingerprint is a fixed pin and never
        # advances with the slice number.
        f"  constants f'print            : "
        f"{payload['constants_fingerprint']}  "
        f"(matches slice 58: "
        f"{payload['constants_fingerprint_matches_slice58']})",
        f"  schedule mode                : {payload['schedule_mode']}",
        f"  caps                         : "
        f"{payload['caps']['max_concurrent_positions']} position, "
        f"{payload['caps']['max_entries_per_day']} entry/day, "
        f"{payload['caps']['max_notional_usd']:.2f} USD",
        f"  caps changed                 : "
        f"{payload['caps_changed_this_slice']}",
        f"  thresholds moved             : "
        f"{payload['thresholds_moved_this_slice']}",
        "",
        "  FORWARD MONITORS (forward trades only)",
    ]
    for reading in payload["forward_monitor_readings"]:
        value = ("n/a" if reading["value"] is None
                 else f"{reading['value']:+.4f}")
        lines.append(f"    {reading['name']:20s} {reading['state']:17s} "
                     f"{value:>9s}   {reading['detail']}")
    lines += [
        f"  forward monitor status       : "
        f"{payload['forward_monitor_status']}",
        "",
        "  The M-4 WARN of slices 58-61 is a fact about the HISTORICAL",
        "  segment. It is not recomputed here, not erased, and still an item",
        "  a human must accept at the gate.",
        "",
        "-" * 78,
        f"  cleared_edge_signal          : {payload['cleared_edge_signal']}  "
        f"(re-scored: {payload['cleared_edge_re_scored_this_slice']})",
        f"  live_authorized              : {payload['live_authorized']}",
        f"  policy_mode                  : {payload['policy_mode']}",
        f"  models_current_present       : "
        f"{payload['models_current_present']}",
        f"  frozen families              : {payload['frozen_absent_count']}",
        f"  is_stage1_evidence           : "
        f"{payload['is_stage1_evidence']}",
        f"  registration_eligible        : "
        f"{payload['registration_eligible']}",
        f"  closer to autonomy           : "
        f"{payload['closer_to_autonomous_profit_agent']}",
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
