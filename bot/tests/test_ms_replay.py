"""ms_replay: the maker-first replay engine end to end on SYNTHETIC series (planted by the test; nothing here is an exchange observation) with the simulated transport only."""
from __future__ import annotations

import json
import math
import os
import sys
from dataclasses import replace

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
for _p in (BOT, os.path.join(BOT, "tools")):
    sys.path.insert(0, _p)

import ms_execution as EX  # noqa: E402
import ms_fillmodel as FM  # noqa: E402
import ms_portfolio as PF  # noqa: E402
import ms_replay as RP  # noqa: E402
import ms_types as T  # noqa: E402

H = 3_600_000
D = 24 * H
T0 = 1_700_000_000_000 // D * D
RATE = 5.0                    # bps per 8h print


def series(sym="AAAUSDT", n_hours=14 * 24, wiggle=0.6, rate=RATE, base=100.0, shock=None, interval=480, offset=0, spot_flat=False):
    marks = []
    for i in range(n_hours + 1):
        px = base + wiggle * math.sin(1.1 * i + 0.3 * sum(map(ord, sym)))
        if shock and shock[0] <= i < shock[1]:
            px = base * shock[2]
        marks.append((T0 + i * H, px))
    funding = tuple((T0 + k * interval * 60_000 + offset, rate) for k in range(1, (n_hours * 60) // interval + 1))
    spot = tuple((T0 + i * H, base + wiggle * math.sin(1.1 * i + 0.3 * sum(map(ord, sym)))) for i in range(n_hours + 1)) if spot_flat else None     # spot ignores the perp shock
    return RP.ReplaySeries(sym, funding, tuple(marks), interval, sym[:-4], spot_mark=spot)


def scan(now, snaps, instruments):
    out = []
    for sym, s in sorted(snaps.items()):
        if s.last_funding_bps_per_print is None or s.last_funding_bps_per_print <= 0:
            continue
        out.append(T.Opportunity(f"c-{sym}", "spec1", "SPOT_PERP_CARRY", now, ((sym, "Sell"), ("SPOT:" + sym, "Buy")), 3.0, 30.0, 0.0, (), 20.0, 1e6, 1.0,
                                 s.last_funding_bps_per_print, "MEASURED"))
    return out


class Risk:
    def __init__(self, ok=True):
        self.ok, self.calls = ok, 0

    def __call__(self, plan, state, policy, snaps):
        self.calls += 1
        return type("D", (), {"approved": self.ok, "reasons": () if self.ok else ("NO",), "limits": {}})()


FILL = FM.FillParams(bar_policy="ASSUME_THROUGH_MULT", bar_through_mult=1.0, markout_mean_bps=1.0)


def cfg(**kw):
    d = dict(start_ms=T0, end_ms=T0 + 12 * D, fill=FILL, hold_ms=2 * D, decision_every_ms=8 * H, variant_id="v0", spec_digest="spec1",
             portfolio=PF.PortfolioPolicy(max_wait_ms=3 * H))
    d.update(kw)
    return RP.ReplayConfig(**d)


def go(series_list=None, c=None, risk=None, scan_fn=scan, **kw):
    return RP.run_replay(series_list or [series()], c or cfg(), scan_fn=scan_fn, risk_assess=risk or Risk(), **kw)


def test_positive_control_a_hedged_carry_is_opened_collects_funding_exits_and_pays_fees():
    r = go()
    obs = RP.observations(r)
    assert obs and r.pnl["funding"] > 0 and r.pnl["fees"] > 0
    f = [x for x in r.fills if not x.fill_id.startswith("FORCED")]
    assert f and all(x.prov in ("MODELLED", "SIMULATED") for x in f)
    assert {x.liquidity for x in f} == {"MAKER", "TAKER"}                   # maker entry, taker exit: the mixed cost
    assert r.pnl["net"] == pytest.approx(r.pnl["price_leg"] + r.pnl["funding"] - r.pnl["fees"] - r.pnl["slippage"])
    assert r.stats["halts"] == [] and all(s in ("CLOSED", "HEDGED", "FAILED", "EXITING") for s in r.states.values())


def test_funding_is_credited_at_the_actual_settlement_timestamps_by_position_side():
    r = go()
    led_events = r.engine and None
    fund = [g.funding_usd for g in r.groups.values()]
    g = next(x for x in r.groups.values() if x.closed)
    perp_entry = [f for f in r.fills if f.group_id == g.group_id and r.fills_cat[f.fill_id] == "linear" and f.side == "Sell" and not f.fill_id.startswith("FORCED")]
    perp_exit = [f for f in r.fills if f.group_id == g.group_id and r.fills_cat[f.fill_id] == "linear" and f.side == "Buy" and not f.fill_id.startswith("FORCED")]
    qty = sum(f.qty for f in perp_entry)
    t_in, t_out = max(f.ts_ms for f in perp_entry), max(f.ts_ms for f in perp_exit)
    prints = [ts for ts, _ in series().funding if t_in < ts <= t_out]
    assert prints and g.funding_usd == pytest.approx(qty * 100.0 * RATE / 1e4 * len(prints), rel=0.01)       # short receives: positive
    assert sum(fund) == pytest.approx(r.pnl["funding"])


def test_a_negative_rate_debits_the_short_the_sign_is_by_position_side():
    s = series(rate=5.0)
    flip = RP.ReplaySeries(s.symbol, tuple((t, -5.0) for t, _ in s.funding), s.mark, s.interval_min, s.base_coin)
    def scan_always(now, snaps, ins):
        return scan(now, {k: replace(v, last_funding_bps_per_print=5.0) for k, v in snaps.items()}, ins)
    r = go([flip], scan_fn=scan_always)
    assert r.pnl["funding"] < 0


def _check_prints(r, ser):
    checked = 0
    for g in r.groups.values():
        if not g.closed or g.kind != "NORMAL":
            continue
        perp_in = [f for f in r.fills if f.group_id == g.group_id and r.fills_cat[f.fill_id] == "linear" and f.side == "Sell"]
        perp_out = [f for f in r.fills if f.group_id == g.group_id and r.fills_cat[f.fill_id] == "linear" and f.side == "Buy"]
        qty, t_in, t_out = sum(f.qty for f in perp_in), max(f.ts_ms for f in perp_in), max(f.ts_ms for f in perp_out)
        prints = [ts for ts, _ in ser.funding if t_in < ts <= t_out]          # strictly after the fill: a print AT the fill instant is not paid
        assert g.funding_usd == pytest.approx(qty * 100.0 * RATE / 1e4 * len(prints), rel=0.02)
        checked += len(prints)
    return checked


def test_funding_prints_follow_each_symbols_own_interval_and_a_print_at_the_fill_instant_is_not_paid():
    s4, s8 = series("BBBUSDT", interval=240), series("AAAUSDT", interval=480)
    n4 = _check_prints(go([s4]), s4)
    n8 = _check_prints(go([s8]), s8)
    assert n4 > 0 and n8 > 0 and n4 > 1.5 * n8 * 0.9


def test_a_funding_print_exactly_at_the_fill_instant_is_not_paid():
    s_al = series("AAAUSDT", offset=H)                                         # prints at hour 9, 17, ...: the first maker fill lands on hour 9
    r = go([s_al])
    fill_ts = {f.ts_ms for f in r.fills}
    assert fill_ts & {ts for ts, _ in s_al.funding}, "the fixture must plant a print on a fill instant"
    assert _check_prints(r, s_al) > 0


def test_a_quote_touch_is_not_a_fill_flat_marks_never_fill_a_post_only_order():
    r = go([series(wiggle=0.0)])
    assert r.fills == [] and r.pnl["net"] == 0.0 and r.pnl["fees"] == 0.0
    assert r.stats["orders_unfilled"] > 0
    assert set(r.states.values()) <= {"FAILED", "SUBMITTED"} and "FAILED" in r.states.values()


def test_bars_without_the_explicit_assumption_never_fill_even_with_a_wiggle():
    r = go(c=cfg(fill=FM.FillParams()))
    assert r.fills == [] and set(r.states.values()) <= {"FAILED", "SUBMITTED"}


def test_no_group_is_ever_opened_when_risk_rejects():
    risk = Risk(ok=False)
    r = go(risk=risk)
    assert risk.calls > 0 and r.groups == {} and r.fills == []


def test_risk_assess_is_required():
    with pytest.raises(ValueError):
        RP.run_replay([series()], cfg(), scan_fn=scan, risk_assess=None)


def test_replay_is_deterministic():
    a, b = go(), go()
    assert a.journal_head == b.journal_head and a.pnl == b.pnl and a.equity_curve == b.equity_curve


def test_report_is_machine_readable_with_the_required_sections_and_never_promotable():
    r = go()
    rep = RP.build_report([r])
    s = RP.report_json(rep)
    assert json.loads(s) == rep
    for k in ("pnl", "execution_mix", "fills", "capacity", "risk", "observations", "by_year", "by_regime", "multiple_testing", "provenance", "promotion_blockers"):
        assert k in rep
    for k in ("gross_usd", "net_usd", "fees_usd", "funding_usd", "price_leg_usd", "slippage_usd", "adverse_selection_usd"):
        assert k in rep["pnl"]
    assert rep["pnl"]["net_usd"] == pytest.approx(r.pnl["net"]) and rep["pnl"]["gross_usd"] == pytest.approx(r.pnl["price_leg"] + r.pnl["funding"])
    assert 0 < rep["execution_mix"]["maker_share_notional"] < 1
    for k in ("max_drawdown_usd", "expected_shortfall_5pct_step_usd", "time_under_water_frac", "sharpe_daily_annualised"):
        assert k in rep["risk"]
    assert rep["promotion_eligible"] is False and any(b.startswith("INDEPENDENT_OBSERVATIONS") for b in rep["promotion_blockers"]) and rep["provenance"]["weakest"] == "ASSUMED"
    assert rep["provenance"]["fills"] == "MODELLED" and "SYNTHETIC_BOOK_DEPTH_AND_SPREAD" in rep["provenance"]["assumed_inputs"]
    assert set(rep["by_year"]) and set(rep["by_regime"])


def test_overlapping_observations_are_never_counted_as_independent():
    syms = [series(f"{c}USDT") for c in "ABC"]
    r = go(syms)
    obs = RP.observations(r)
    assert len(obs) >= 3
    ind = RP.independent_observations(obs)
    assert len(ind) < len(obs) and all(a.exit_ts < b.entry_ts for a, b in zip(ind, ind[1:]))
    rep = RP.build_report([r])
    assert rep["observations"]["independent"] == len(ind) and rep["observations"]["dropped_as_overlapping"] == len(obs) - len(ind)
    # a hand-built check of the rule itself
    mk = lambda i, a, b: RP.Observation(f"g{i}", ("X",), a, b, 1.0, 1.0, "ALL", "NORMAL")
    assert [o.group_id for o in RP.independent_observations([mk(1, 0, 10), mk(2, 5, 15), mk(3, 11, 20), mk(4, 20, 30)])] == ["g1", "g3"]
    assert [o.group_id for o in RP.independent_observations([mk(1, 0, 10), mk(3, 11, 20)], embargo_ms=5)] == ["g1"]


def test_ci_is_none_with_fewer_than_two_independent_observations_and_widens_with_trials():
    assert RP._ci([1.0], 1)["lo95"] is None
    vals = [1.0, 2.0, 3.0, 4.0, 2.5, 3.5] * 6
    a, b = RP._ci(vals, 1), RP._ci(vals, 100)
    assert (b["hi_adj"] - b["lo_adj"]) > (a["hi_adj"] - a["lo_adj"]) and a["reliable"]
    assert not RP._ci(vals[:5], 1)["reliable"]


# ---------------------------------------------------------------- walk-forward, purge/embargo, trials ledger

def test_walk_forward_splits_are_purged_embargoed_and_disjoint():
    sp = RP.walk_forward_splits(0, 100 * D, n_folds=3, test_ms=10 * D, purge_ms=3 * D, embargo_ms=1 * D)
    assert [(s.test_start // D, s.test_end // D) for s in sp] == [(70, 80), (80, 90), (90, 100)]
    for s in sp:
        assert s.test_start - s.train_end == 4 * D and s.train_start == 0
    assert all(a.test_end <= b.test_start for a, b in zip(sp, sp[1:]))
    with pytest.raises(ValueError):
        RP.walk_forward_splits(0, 12 * D, n_folds=3, test_ms=5 * D, purge_ms=3 * D, embargo_ms=1 * D)


def test_walk_forward_selects_on_train_runs_test_once_reports_rejected_and_counts_trials(tmp_path):
    ser = [series(n_hours=40 * 24)]
    v_good = RP.Variant("good", cfg(variant_id="good", spec_digest="s-good"), scan)
    v_none = RP.Variant("none", cfg(variant_id="none", spec_digest="s-none"), lambda now, snaps, ins: [])
    splits = RP.walk_forward_splits(T0, T0 + 40 * D, n_folds=2, test_ms=8 * D, purge_ms=2 * D, embargo_ms=D)
    path = str(tmp_path / "trials.jsonl")
    rep = RP.run_walk_forward(ser, [v_good, v_none], splits, risk_assess=Risk(), trials_path=path)
    folds = rep["variants"]["folds"]
    assert [f["selected"] for f in folds] == ["good", "good"] and all(f["rejected"] == ["none"] for f in folds)
    assert rep["multiple_testing"] == {"trials_this_run": 2, "trials_total": 2, "persistent_ledger": True, "note": rep["multiple_testing"]["note"]}
    assert rep["window"]["n_windows"] == 2 and rep["promotion_eligible"] is False
    rep2 = RP.run_walk_forward(ser, [v_good, RP.Variant("third", cfg(variant_id="third", spec_digest="s-3"), scan)], splits, risk_assess=Risk(), trials_path=path)
    assert rep2["multiple_testing"]["trials_total"] == 3 and rep2["multiple_testing"]["trials_this_run"] == 2         # 'good' is not re-counted, 'third' is new
    assert RP.record_trials(path, ["x"]) == 4 and RP.record_trials(path, ["x"]) == 4


def test_walk_forward_refuses_a_purge_shorter_than_the_holding_period():
    v = RP.Variant("v", cfg(hold_ms=5 * D), scan)
    sp = RP.walk_forward_splits(T0, T0 + 40 * D, n_folds=1, test_ms=8 * D, purge_ms=D, embargo_ms=0)
    with pytest.raises(ValueError):
        RP.run_walk_forward([series(n_hours=40 * 24)], [v], sp, risk_assess=Risk())


def test_nothing_is_selected_when_no_variant_has_a_train_round_trip():
    v = RP.Variant("none", cfg(variant_id="none"), lambda now, snaps, ins: [])
    sp = RP.walk_forward_splits(T0, T0 + 30 * D, n_folds=1, test_ms=6 * D, purge_ms=2 * D, embargo_ms=0)
    rep = RP.run_walk_forward([series(n_hours=30 * 24)], [v], sp, risk_assess=Risk())
    assert rep["variants"]["folds"][0]["selected"] is None and rep["promotion_blockers"] == ["NO_TEST_WINDOW_RAN"]


# ---------------------------------------------------------------- fault injection through the whole replay

def faulted(*faults, **kw):
    return go(c=cfg(faults=tuple(faults), **kw))


def test_fault_lost_ack_is_reconciled_without_a_second_order():
    r = faulted(RP.Fault(T0, "LOST_ACK"))
    assert r.fault_log and max(r.venue.send_calls.values()) == 1
    assert any(e["type"] == "ACK_LOST" for e in r.engine.journal.entries) and RP.observations(r)


def test_fault_lost_request_is_resent_at_most_once():
    r = faulted(RP.Fault(T0, "LOST_REQUEST"))
    calls = r.venue.send_calls
    assert any(e["type"] == "ACK_LOST" for e in r.engine.journal.entries) and max(calls.values()) <= 2 and sum(1 for v in calls.values() if v == 2) <= 1


def test_fault_duplicate_fill_changes_nothing():
    base = go()
    r = faulted(RP.Fault(T0 + 9 * H, "DUPLICATE_FILL"))
    assert r.stats["dup_fills_injected"] == 1 and r.stats["fill_dups"] >= 1
    assert r.pnl["net"] == pytest.approx(base.pnl["net"]) and r.pnl["fees"] == pytest.approx(base.pnl["fees"])


def test_fault_duplicate_ack_is_ignored():
    r = faulted(RP.Fault(T0, "DUPLICATE_ACK"))
    assert r.stats["ack_dups"] >= 1 and RP.observations(r)


def test_fault_stale_book_refuses_placements_and_nothing_trades_during_it():
    r = faulted(RP.Fault(T0, "STALE_BOOK", until_ms=T0 + 3 * D))
    early = [d for d in r.decisions if d["ts_ms"] <= T0 + 3 * D]
    assert early and all(d["action"] == "SKIP" and d["reasons"][0] == "MISSING_OR_STALE_BOOK:AAAUSDT" for d in early)
    assert min([f.ts_ms for f in r.fills] or [10 ** 18]) > T0 + 3 * D and RP.observations(r)


def test_fault_stale_account_blocks_all_new_exposure():
    r = faulted(RP.Fault(T0, "STALE_ACCOUNT", until_ms=T0 + 2 * D))
    early = [d for d in r.decisions if d["ts_ms"] <= T0 + 2 * D]
    assert early and all(d["action"] == "SKIP" and d["reasons"][0] == "BLOCK_ACCOUNT_TS_STALE" for d in early)
    assert min([f.ts_ms for f in r.fills] or [10 ** 18]) > T0 + 2 * D


def test_fault_unknown_position_halts_new_exposure_then_lifts():
    r = faulted(RP.Fault(T0 + 1, "POSITIONS_UNKNOWN", until_ms=T0 + 3 * D))
    ev = [(e["type"], e["body"].get("reason"), e["ts_ms"]) for e in r.engine.journal.entries if e["type"] in ("HALT_NEW", "RESUME_NEW")]
    assert ("HALT_NEW", "UNKNOWN_VENUE_POSITION", T0 + H) in ev and any(t == "RESUME_NEW" and ts > T0 + 3 * D for t, _, ts in ev)
    assert r.engine.halts == {} and min([f.ts_ms for f in r.fills] or [10 ** 18]) > T0 + 3 * D and RP.observations(r)


def test_fault_instrument_halt_blocks_the_symbol_until_resume():
    r = faulted(RP.Fault(T0 + 4 * D, "INSTRUMENT_HALT", symbol="AAAUSDT", until_ms=T0 + 6 * D))
    assert r.engine.halted_symbols == set()
    assert not [f for f in r.fills if T0 + 4 * D < f.ts_ms <= T0 + 6 * D and f.liquidity == "MAKER"]
    assert any(e["type"] == "SYMBOL_HALT" for e in r.engine.journal.entries) and any(e["type"] == "SYMBOL_RESUME" for e in r.engine.journal.entries)


def test_fault_funding_interval_change_blocks_new_entries_on_the_symbol():
    r = faulted(RP.Fault(T0 + 4 * D, "INTERVAL_CHANGE", symbol="AAAUSDT", value=60))
    assert "AAAUSDT" in r.engine.blocked_symbols
    last = max([f.ts_ms for f in r.fills if r.fills_cat[f.fill_id] == "linear" and f.side == "Sell"] or [0])
    assert last < T0 + 4 * D + 8 * H


def test_fault_risk_breach_halts_new_exposure_for_the_rest_of_the_run():
    r = faulted(RP.Fault(T0 + 3 * D, "RISK_BREACH"))
    assert any(h.startswith("RISK_BREACH") for h in r.engine.halts)
    assert max([f.ts_ms for f in r.fills if f.liquidity == "MAKER"] or [0]) < T0 + 3 * D + 8 * H


def test_fault_restart_rebuilds_from_the_journal_and_never_duplicates_orders():
    base = go()
    r = faulted(RP.Fault(T0 + 2 * D + 30 * 60_000, "RESTART"), RP.Fault(T0 + 5 * D, "RESTART"))
    assert max(r.venue.send_calls.values()) == 1 and r.states == base.states and r.pnl["net"] == pytest.approx(base.pnl["net"])
    assert [e["type"] for e in r.engine.journal.entries].count("PLAN") == [e["type"] for e in base.engine.journal.entries].count("PLAN")


def test_fault_cancel_lost_is_reconciled():
    r = faulted(RP.Fault(T0, "CANCEL_LOST"))
    lost = [e for e in r.engine.journal.entries if e["type"] == "CANCEL_DONE" and e["body"]["status"] == "LOST"]
    assert lost and not any(g.state.value == "UNKNOWN_OUTCOME" for g in r.engine.groups.values()) and max(r.venue.send_calls.values()) == 1


def test_liquidation_closes_the_perp_unwinds_the_spot_and_is_reported():
    s = series(shock=(24 * 6, 24 * 12, 1.8), wiggle=0.3, spot_flat=True)
    r = go([s], c=cfg(hold_ms=20 * D, end_ms=T0 + 13 * D))
    assert r.stats["liquidations"] >= 1
    g = next(iter(r.groups.values()))
    assert g.kind == "UNWOUND" and g.closed and r.pnl["net"] < 0
    assert RP.build_report([r])["risk"]["liquidations"] >= 1


def test_paper_mode_injects_a_clock_and_still_labels_fills_modelled():
    ticks = iter(range(T0, T0 + 20 * D, H))
    r = RP.run_replay([series()], cfg(end_ms=T0 + 6 * D), scan_fn=scan, risk_assess=Risk(), clock=lambda: next(ticks))
    assert r.fills and all(f.prov in ("MODELLED", "SIMULATED") for f in r.fills)


# ---------------------------------------------------------------- per-print funding intervals in the snapshot (contract with the scanner)

CHANGE = T0 + 4 * D                       # 480 -> 240 minute prints; the funding is a TRUE flat 15 bps/day throughout


def changing_series(sym="AAAUSDT", segments=True, interval_min=240, only_new_segment=False):
    marks = tuple((T0 + i * H, 100.0 + 0.3 * math.sin(1.1 * i)) for i in range(0, 14 * 24 + 1))
    old = tuple((T0 + k * 480 * 60_000, 5.0) for k in range(1, 4 * 3 + 1))                       # 3 prints/day x 5 bps = 15 bps/day
    new = tuple((CHANGE + k * 240 * 60_000, 2.5) for k in range(1, 10 * 6 + 1))                  # 6 prints/day x 2.5 bps = 15 bps/day
    segs = ((new[0][0], new[-1][0], 240),) if only_new_segment else ((old[0][0], old[-1][0], 480), (new[0][0], new[-1][0], 240))
    return RP.ReplaySeries(sym, old + new, marks, interval_min, sym[:-4], funding_intervals=segs if segments else None)


def record_snaps():
    seen = {}

    def scan_fn(now, snaps, instruments):
        seen[now] = dict(snaps)
        return []
    return seen, scan_fn


def check_snapshot_carries_the_interval_of_each_print(snap):
    ivs = snap.trailing_funding_intervals_min
    assert len(ivs) == len(snap.trailing_funding_bps_per_print) > 0
    assert 480 in ivs and 240 in ivs and ivs == tuple(sorted(ivs, reverse=True))                # the change is INSIDE the trailing window, oldest -> newest
    per_day = [T.funding_bps_per_day(r, iv) for r, iv in zip(snap.trailing_funding_bps_per_print, ivs)]
    assert per_day == pytest.approx([15.0] * len(ivs))                                           # each print normalised by ITS interval: a true flat 15 bps/day
    assert snap.funding_interval_min == 240                                                      # the interval in force at the decision is a different field
    wrong = [T.funding_bps_per_day(r, snap.funding_interval_min) for r in snap.trailing_funding_bps_per_print]
    assert wrong != pytest.approx([15.0] * len(wrong))                                          # normalising every print by the CURRENT interval would NOT give 15


def test_the_snapshot_carries_the_funding_interval_in_force_at_each_trailing_print():
    seen, fn = record_snaps()
    go([changing_series()], c=cfg(end_ms=CHANGE + 2 * D), scan_fn=fn)
    t = CHANGE + D                                                                                # 6 prints at 240 and 6 at 480 in the 12-print window
    check_snapshot_carries_the_interval_of_each_print(seen[t]["AAAUSDT"])


def test_mutation_filling_the_intervals_with_the_current_one_is_caught(monkeypatch):
    monkeypatch.setattr(RP.World, "trailing_funding_intervals", lambda self, sym, t, n=12: tuple(self.interval_min(sym, t) for _ in self.trailing_funding(sym, t, n)))
    seen, fn = record_snaps()
    go([changing_series()], c=cfg(end_ms=CHANGE + 2 * D), scan_fn=fn)
    t = CHANGE + D
    with pytest.raises(AssertionError):
        check_snapshot_carries_the_interval_of_each_print(seen[t]["AAAUSDT"])


def test_a_series_with_one_known_interval_and_no_segments_uses_that_interval_for_every_print():
    s = series(interval=240)
    seen, fn = record_snaps()
    go([s], c=cfg(end_ms=T0 + 3 * D), scan_fn=fn)
    snap = seen[T0 + 2 * D]["AAAUSDT"]
    assert snap.trailing_funding_intervals_min == (240,) * len(snap.trailing_funding_bps_per_print) and len(snap.trailing_funding_bps_per_print) > 0


@pytest.mark.parametrize("kind", ["no_interval_no_segments", "prints_before_the_first_segment"])
def test_an_unknown_print_interval_leaves_the_tuple_empty_never_the_current_interval(kind):
    s = replace(series(), interval_min=None) if kind == "no_interval_no_segments" else changing_series(only_new_segment=True)
    seen, fn = record_snaps()
    go([s], c=cfg(end_ms=CHANGE + 2 * D), scan_fn=fn)
    window = [seen[k]["AAAUSDT"] for k in sorted(seen) if CHANGE + 8 * H <= k < CHANGE + 2 * D] if kind != "no_interval_no_segments" else [seen[k]["AAAUSDT"] for k in sorted(seen) if k > T0 + 2 * D]
    assert window and all(s_.trailing_funding_bps_per_print and s_.trailing_funding_intervals_min == () for s_ in window)
    # once every print in the window is inside a known segment the intervals are back
    if kind != "no_interval_no_segments":
        late = seen[max(seen)]["AAAUSDT"]
        assert late.trailing_funding_intervals_min == (240,) * len(late.trailing_funding_bps_per_print)


def test_an_unknown_interval_symbol_is_not_allocated_by_a_scan_that_honours_the_contract():
    def contract_scan(now, snaps, instruments):
        out = []
        for sym, s in sorted(snaps.items()):
            if not s.trailing_funding_intervals_min or len(s.trailing_funding_intervals_min) != len(s.trailing_funding_bps_per_print):
                continue                                                                          # FUNDING_INTERVAL_PER_PRINT_UNKNOWN: excluded, never normalised with the current interval
            out.append(T.Opportunity(f"c-{sym}", "spec1", "SPOT_PERP_CARRY", now, ((sym, "Sell"), ("SPOT:" + sym, "Buy")), 3.0, 30.0, 0.0, (), 20.0, 1e6, 1.0,
                                     s.last_funding_bps_per_print, "MEASURED"))
        return out
    known, unknown = changing_series("AAAUSDT"), changing_series("BBBUSDT", only_new_segment=True)
    r = go([known, unknown], c=cfg(end_ms=CHANGE + 6 * D), scan_fn=contract_scan)
    traded = {s for g in r.groups.values() for s in g.symbols}
    assert "AAAUSDT" in traded
    early = [g for g in r.groups.values() if "BBBUSDT" in g.symbols and g.entry_ts is not None and g.entry_ts < CHANGE + 2 * D]
    assert early == []                                                                         # not allocated while ANY print in its window has an unknown interval
    assert any("BBBUSDT" in g.symbols for g in r.groups.values())                              # positive control: allocated once every print in the window is known


def test_local_ledger_values_open_legs_at_the_exit_side_less_the_exit_fee_and_flags_a_missing_book():
    led = RP.LocalLedger(exit_fee_bps_perp=5.5, exit_fee_bps_spot=10.0)
    led.record_fill(T.Fill("f1", "G", "P", "XUSDT", "Sell", 10.0, 100.0, 0.02, "USDT", "MAKER", T0, "SIMULATED"), category="linear")
    led.record_fill(T.Fill("f2", "G", "S", "XUSDT", "Buy", 10.0, 100.0, 0.02, "USDT", "MAKER", T0, "SIMULATED"), category="spot")
    led.mark("XUSDT", T0 + 1, 100.0)
    led.mark("SPOT:XUSDT", T0 + 1, 100.0)
    p0 = led.pnl()
    assert p0["unrealised"] == pytest.approx(0.0) and p0["valuation"] == "MID_FALLBACK" and p0["flags"] == ["MID_FALLBACK"]      # no book: the mark, and SAID so
    led.mark("XUSDT", T0 + 2, 100.0, bid=99.5, ask=100.5)                    # short perp: closes by buying at the ASK
    led.mark("SPOT:XUSDT", T0 + 2, 100.0, bid=99.8, ask=100.2)               # long spot: closes by selling at the BID
    p = led.pnl()
    want = (100.0 - 100.5) * 10 - 10 * 100.5 * 5.5 / 1e4 + (99.8 - 100.0) * 10 - 10 * 99.8 * 10.0 / 1e4
    assert p["valuation"] == "EXIT_SIDE" and p["flags"] == []
    assert p["unrealised"] == pytest.approx(want) and p["net"] == pytest.approx(p0["net"] + want)
    RP.assert_net_identity(p)


def test_replay_values_open_positions_at_the_exit_side_in_the_ledger_the_group_records_and_the_report():
    r = go(c=cfg(end_ms=T0 + 5 * D, hold_ms=10 * D))
    assert r.pnl["valuation"] == "EXIT_SIDE" and r.pnl["unrealised"] not in (None, 0.0) and r.pnl["unrealised"] < 0      # crossing the spread + exit fees costs money
    RP.assert_net_identity(r.pnl)
    net = sum(g.price_usd + g.funding_usd - g.fees_usd - g.adverse_usd for g in r.groups.values())
    assert net == pytest.approx(r.pnl["net"], abs=1e-6)                     # the per-group end-of-run valuation is the SAME exit-side one
    assert not any(g.mid_fallback for g in r.groups.values())
    assert RP.build_report([r])["pnl"]["valuation"] == "EXIT_SIDE"


# ------------------------------------------------------------------------------------------------ a taker walks the book it SEES: a book of any age is not a quote

def _stale_world(book_ts, **cfg_kw):
    import ms_execution as EXX
    import ms_fillmodel as FMM
    import ms_types as TT
    t0 = 1_700_000_000_000
    marks = tuple((t0 + i * 3_600_000, 100.0) for i in range(0, 30))
    bk = ((book_ts, (TT.BookLevel(99.99, 1e6),), (TT.BookLevel(100.01, 1e6),)),)
    s = RP.ReplaySeries("XXXUSDT", ((t0 + 8 * 3_600_000, 1.0),), marks, 480, "XXX", books=bk)
    cfg = RP.ReplayConfig(start_ms=t0, end_ms=t0 + 20 * 3_600_000, fill=FMM.FillParams(), **cfg_kw)
    w = RP.World([s], cfg)
    tr = RP.ReplayTransport(w, cfg)
    return t0, tr, EXX, TT


def _req(EXX, TT, side, *, reduce_only, link):
    return EXX.OrderRequest("g1", "c1", "d" * 8, TT.LegPlan("l1", "XXXUSDT", "linear", side, "1", "Market", None, reduce_only=reduce_only), link)


def check_stale_taker(book_age_ms, *, exit_cap=None):
    kw = {} if exit_cap is None else {"exit_book_max_age_ms": exit_cap}
    t0, tr, EXX, TT = _stale_world(None or 1_700_000_000_000, **kw)
    tr.now = t0 + book_age_ms
    ack = tr.send_order(_req(EXX, TT, "Sell", reduce_only=False, link="g1-e1"))
    entry_filled = bool(tr.pending)
    tr.venue.book["linear:XXXUSDT"] = -1.0
    tr.pending.clear()
    ack_x = tr.send_order(_req(EXX, TT, "Buy", reduce_only=True, link="g1-x1"))
    return ack, entry_filled, ack_x, bool(tr.pending), tr.stats


def test_a_taker_entry_against_a_six_hour_old_book_is_refused_and_a_fresh_book_fills():
    ack, filled, _ax, _xf, stats = check_stale_taker(6 * 3_600_000)
    assert ack.status == "REJECTED" and ack.reason == "STALE_QUOTE" and not filled and stats["refused"]["STALE_QUOTE"] >= 1
    ack, filled, *_ = check_stale_taker(2_000)                                                 # positive control: a fresh book fills
    assert ack.status != "REJECTED" and filled


def test_a_reduce_only_exit_may_use_only_the_explicit_wider_cap_and_it_is_counted():
    _a, _f, ax, xf, stats = check_stale_taker(6 * 3_600_000)                                  # default cap 1 h: a 6 h book is refused for exits too
    assert ax.status == "REJECTED" and ax.reason == "STALE_QUOTE" and not xf and stats["exits_refused_stale_quote"] == 1
    _a, _f, ax, xf, stats = check_stale_taker(6 * 3_600_000, exit_cap=12 * 3_600_000)         # an explicit, configured wider cap: the exit fills and the use is counted, never silent
    assert ax.status != "REJECTED" and xf and stats["exits_on_wider_book_cap"] == 1
    _a, _f, ax, xf, stats = check_stale_taker(2_000)                                          # fresh: no cap used
    assert xf and stats.get("exits_on_wider_book_cap", 0) == 0


def test_mutation_a_taker_that_ignores_book_age_is_caught(monkeypatch):
    check_stale_taker(6 * 3_600_000)
    real = RP.World.book
    monkeypatch.setattr(RP.World, "book", lambda self, sym, cat, t: real(self, sym, cat, t)[:2] + (0,))           # the transport sees every book as fresh
    with pytest.raises(AssertionError):
        test_a_taker_entry_against_a_six_hour_old_book_is_refused_and_a_fresh_book_fills()


# ------------------------------------------------------------------------------------------------ an UNKNOWN price or fee is never zero in the replay ledger
def _fill(i, side, qty, price, fee=0.1, prov="SIMULATED", symbol="XUSDT"):
    return T.Fill(f"f{i}", "g", f"l{i}", symbol, side, qty, price, fee, "USDT", "TAKER", T0 + i, prov)


def test_an_open_short_with_no_mark_is_valued_at_cost_not_at_zero():
    """Valued at 0, a short's whole sale (here $1,000) was booked as profit (+$998.70 on a $1,000 pair in the capital audit): an unknown price is not zero."""
    led = RP.LocalLedger()
    led.record_fill(_fill(1, "Sell", 10.0, 100.0, fee=0.55))                  # short perp, never marked
    p = led.pnl()
    assert p["flags"] == ["MID_FALLBACK", "MARK_MISSING:XUSDT"] and p["valuation"] == "MID_FALLBACK"
    assert p["unrealised"] == pytest.approx(-1000.0)                         # 10 x -100: valued at what it was sold for, no mark-to-market P&L
    assert p["net"] == pytest.approx(-0.55)                                  # the fee is the only P&L: no phantom +$1,000
    led.mark("XUSDT", T0 + 5, 100.0)                                         # a mark arrives: the flag goes, the valuation is the mark
    q = led.pnl()
    assert q["flags"] == ["MID_FALLBACK"] and q["net"] == pytest.approx(-0.55)


def test_a_partially_closed_position_with_no_mark_keeps_its_realised_gain_at_average_cost():
    led = RP.LocalLedger()
    led.record_fill(_fill(1, "Buy", 1.0, 100.0, fee=0.0, prov="MEASURED"))
    led.record_fill(_fill(2, "Sell", 0.4, 110.0, fee=0.0, prov="MEASURED"))   # realised +4 on the 0.4 sold; 0.6 remain open and unmarked
    p = led.pnl()
    assert "MARK_MISSING:XUSDT" in p["flags"] and p["net"] == pytest.approx(4.0)             # the open 0.6 at its average cost 100 adds nothing
    led.record_fill(_fill(3, "Sell", 1.0, 90.0, fee=0.0, prov="MEASURED"))   # flips short 0.4: the remainder was opened at 90
    p2 = led.pnl()
    assert p2["net"] == pytest.approx(4.0 + 0.6 * (90.0 - 100.0) + 0.0)                       # closes 0.6 at 90 (-6); the new 0.4 short is valued at its own 90 cost


def test_an_unverified_zero_fee_is_flagged_but_a_measured_zero_fee_is_not():
    led = RP.LocalLedger()
    led.record_fill(_fill(1, "Buy", 1.0, 100.0, fee=0.0, prov="SIMULATED"))
    assert led.pnl()["fee_flags"] == ["ZERO_FEE_UNVERIFIED"]
    meas = RP.LocalLedger()
    meas.record_fill(_fill(1, "Buy", 1.0, 100.0, fee=0.0, prov="MEASURED"))
    assert meas.pnl()["fee_flags"] == []
    priced = RP.LocalLedger()
    priced.record_fill(_fill(1, "Buy", 1.0, 100.0, fee=0.055, prov="SIMULATED"))
    assert priced.pnl()["fee_flags"] == []


def test_adds_in_the_same_direction_move_the_average_cost_for_longs_and_shorts():
    """Unmarked, a position is valued at its AVERAGE cost, so the only P&L is the fees (the same-direction add path: weighted by the absolute open quantity)."""
    for side, sign in (("Buy", 1.0), ("Sell", -1.0)):
        led = RP.LocalLedger()
        led.record_fill(_fill(1, side, 10.0, 100.0, fee=0.5, prov="MEASURED"))
        led.record_fill(_fill(2, side, 10.0, 110.0, fee=0.5, prov="MEASURED"))          # average 105
        p = led.pnl()
        assert "MARK_MISSING:XUSDT" in p["flags"] and p["net"] == pytest.approx(-1.0), side      # fees only: no phantom gain or loss from the unknown mark
        assert p["unrealised"] == pytest.approx(sign * 20.0 * 105.0)


def test_an_unverified_rebate_is_flagged_like_the_canonical_ledger_does():
    led = RP.LocalLedger()
    led.record_fill(_fill(1, "Buy", 1.0, 100.0, fee=-0.5, prov="SIMULATED"))
    assert "FEE_REBATE_UNVERIFIED" in led.pnl()["fee_flags"]


def test_the_report_carries_the_unknown_input_flags_so_a_consumer_cannot_miss_them():
    r = go(c=cfg(end_ms=T0 + 5 * D, hold_ms=10 * D))
    assert RP.build_report([r])["pnl"]["unknown_input_flags"] == []                        # a complete, priced replay has none
    r.pnl["flags"] = list(r.pnl.get("flags") or []) + ["MARK_MISSING:XUSDT"]
    r.pnl["fee_flags"] = ["ZERO_FEE_UNVERIFIED"]
    assert RP.build_report([r])["pnl"]["unknown_input_flags"] == ["MARK_MISSING:XUSDT", "ZERO_FEE_UNVERIFIED"]


# ---------------------------------------------------------------- an incomplete net is a SUBTOTAL: never published as THE net, never ranked
def test_a_complete_run_publishes_its_net_and_says_so():
    r = go()
    rep = RP.build_report([r])
    assert rep["pnl"]["net_complete"] is True and rep["pnl"]["net_usd"] == rep["pnl"]["net_subtotal_usd"] and rep["pnl"]["net_usd"] is not None


def test_default_score_never_ranks_an_incomplete_run(monkeypatch):
    import types
    monkeypatch.setattr(RP, "observations", lambda r: [object()])
    c = types.SimpleNamespace(equity0=1000.0)
    inc = types.SimpleNamespace(cfg=c, equity_curve=[(1, 1010.0)], pnl={"net": 50.0, "net_complete": False})
    ok = types.SimpleNamespace(cfg=c, equity_curve=[(1, 1010.0)], pnl={"net": 50.0, "net_complete": True})
    assert RP.default_score(inc) == float("-inf") and RP.default_score(ok) == pytest.approx(50.0)


def test_the_report_withholds_the_net_of_an_incomplete_window_and_keeps_its_subtotal():
    r = go()
    inc = replace(r, pnl={**r.pnl, "net_complete": False, "fee_flags": ["ZERO_FEE_UNVERIFIED"]})
    rep = RP.build_report([inc])
    assert rep["pnl"]["net_complete"] is False and rep["pnl"]["net_usd"] is None and rep["pnl"]["net_subtotal_usd"] == pytest.approx(r.pnl["net"])
    assert "ZERO_FEE_UNVERIFIED" in rep["pnl"]["unknown_input_flags"]
