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
