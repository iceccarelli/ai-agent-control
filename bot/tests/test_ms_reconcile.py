"""ms_reconcile: typed findings for every disagreement between the canonical position truth and its derived views, the halt hook, and the narrow reservation release. Pure, offline."""
import os
import sys
from decimal import Decimal as D

import pytest

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BOT)
sys.path.insert(0, os.path.join(BOT, "tools"))

import ms_portfolio as PF  # noqa: E402
import ms_reconcile as RC  # noqa: E402

NOW = 1_800_000_000_000
PAIR_ENGINE = {"linear:BTCUSDT": "-10", "spot:BTCUSDT": "10"}
PAIR_CANON = {"BTCUSDT": D(-10), "SPOT:BTCUSDT": D(10)}


def views(**kw):
    d = dict(engine_positions=dict(PAIR_ENGINE), ledger_positions=dict(PAIR_CANON), venue_positions={"linear:BTCUSDT": -10.0, "spot:BTCUSDT": 10.0}, venue_complete=True)
    d.update(kw)
    return RC.Views(**d)


def codes(rep):
    return sorted(rep.codes())


def test_positive_control_all_views_agree_no_halt():
    rep = RC.reconcile(views())
    assert rep.ok and rep.halt_new_exposure is False and rep.reasons == () and rep.compared["reference"] == "venue"
    assert rep.to_dict()["ok"] is True


@pytest.mark.parametrize("k,want", [("linear:BTCUSDT#t", "BTCUSDT"), ("BTCUSDT", "BTCUSDT"), ("spot:BTCUSDT", "SPOT:BTCUSDT"), ("SPOT:BTCUSDT#pending:x", "SPOT:BTCUSDT"), ("", None), (3, None)])
def test_canon_key(k, want):
    assert RC.canon_key(k) == want


def test_spelling_cannot_hide_a_position_tags_and_prefixes_fold_together():
    rep = RC.reconcile(views(engine_positions={"BTCUSDT#a": "-4", "linear:BTCUSDT#b": "-6", "SPOT:BTCUSDT": 10}))
    assert rep.ok


def test_venue_shows_extra_qty_is_a_qty_mismatch_naming_both_views():
    rep = RC.reconcile(views(venue_positions={"linear:BTCUSDT": -12.0, "spot:BTCUSDT": 10.0}))
    f = [x for x in rep.findings if x.code == RC.QTY_MISMATCH]
    assert rep.halt_new_exposure and {x.key for x in f} == {"BTCUSDT"} and {v for x in f for v in x.views} == {"venue", "engine", "ledger"}


def test_ledger_missing_a_position_is_missing_in_ledger_and_halts():
    rep = RC.reconcile(views(ledger_positions={"SPOT:BTCUSDT": D(10)}))
    assert [(f.code, f.key) for f in rep.findings] == [(RC.MISSING_IN_LEDGER, "BTCUSDT")] and rep.halt_new_exposure
    rep = RC.reconcile(views(ledger_positions={"BTCUSDT": D(0), "SPOT:BTCUSDT": D(10)}))
    assert rep.codes() == (RC.MISSING_IN_LEDGER,)                                          # a zero is not the position either


def test_position_the_venue_does_not_have_is_missing_in_venue():
    rep = RC.reconcile(views(venue_positions={"spot:BTCUSDT": 10.0}))
    assert RC.MISSING_IN_VENUE in rep.codes() and rep.halt_new_exposure


def test_opposite_sides_are_a_position_mismatch_not_a_qty_mismatch():
    rep = RC.reconcile(views(engine_positions={"linear:BTCUSDT": "10", "spot:BTCUSDT": "10"}))
    assert RC.POSITION_MISMATCH in rep.codes() and RC.QTY_MISMATCH not in rep.codes()


def test_engine_flat_while_the_venue_holds_is_a_position_mismatch():
    rep = RC.reconcile(views(engine_positions={}))
    assert {f.key for f in rep.findings if f.code == RC.POSITION_MISMATCH} == {"BTCUSDT", "SPOT:BTCUSDT"}


@pytest.mark.parametrize("kw,detail", [
    ({"venue_positions": None}, "NO_VENUE_POSITION_READ"),
    ({"venue_complete": False}, "READ_NOT_COMPLETE"),
    ({"venue_positions": {"linear:BTCUSDT": None, "spot:BTCUSDT": 10.0}}, "UNKNOWN_QTY"),
    ({"venue_positions": [1]}, "VENUE_READ_NOT_A_MAPPING"),
])
def test_an_incomplete_or_missing_venue_read_halts_and_never_counts_as_agreement(kw, detail):
    rep = RC.reconcile(views(**kw))
    assert rep.halt_new_exposure and RC.INCOMPLETE_VENUE_READ in rep.codes() and any(detail in f.detail for f in rep.findings)
    assert rep.compared["reference"] in ("engine", None)                                      # the engine is the fallback reference so the OTHER disagreements are still named


def test_a_missing_venue_is_only_tolerated_when_explicitly_not_required():
    assert RC.reconcile(views(venue_positions=None), require_venue=False).ok


def test_incomplete_venue_read_still_names_other_disagreements():
    rep = RC.reconcile(views(venue_complete=False, ledger_positions={"SPOT:BTCUSDT": D(10)}))
    assert set(rep.codes()) == {RC.INCOMPLETE_VENUE_READ, RC.MISSING_IN_LEDGER}


def test_spot_baseline_the_client_already_owned_is_not_a_mismatch():
    v = views(venue_positions={"linear:BTCUSDT": -10.0, "spot:BTCUSDT": 15.0}, engine_spot_baseline={"spot:BTCUSDT": "5"})
    assert RC.reconcile(v).ok
    assert RC.QTY_MISMATCH in RC.reconcile(views(venue_positions={"linear:BTCUSDT": -10.0, "spot:BTCUSDT": 15.0})).codes()


def test_tolerance_is_stated_and_enforced():
    assert RC.reconcile(views(venue_positions={"linear:BTCUSDT": -10.0000000001, "spot:BTCUSDT": 10.0})).ok
    assert not RC.reconcile(views(venue_positions={"linear:BTCUSDT": -10.001, "spot:BTCUSDT": 10.0})).ok
    assert RC.reconcile(views(venue_positions={"linear:BTCUSDT": -10.001, "spot:BTCUSDT": 10.0}), qty_tol=D("0.01")).ok


def test_artifact_positions_are_compared_too():
    assert RC.reconcile(views(artifact_positions={"BTCUSDT": -10, "SPOT:BTCUSDT": 10})).ok
    rep = RC.reconcile(views(artifact_positions={"BTCUSDT": -9, "SPOT:BTCUSDT": 10}))
    assert [(f.code, f.views[1]) for f in rep.findings] == [(RC.QTY_MISMATCH, "artifact")]


def pstate(**pos):
    return PF.PortfolioState(equity_usd=1e5, positions={k: PF.Position(k, "spot" if k.startswith("SPOT:") else "linear", n) for k, n in pos.items()})


def test_portfolio_is_compared_by_presence_sign_and_by_quantity_when_marked():
    ok = pstate(BTCUSDT=-1000.0, **{"SPOT:BTCUSDT": 1000.0})
    assert RC.reconcile(views(portfolio=ok, portfolio_marks={"BTCUSDT": 100, "SPOT:BTCUSDT": 100})).ok
    assert RC.reconcile(views(portfolio=ok)).ok                                              # no marks: sign / presence only
    assert RC.QTY_MISMATCH in RC.reconcile(views(portfolio=pstate(BTCUSDT=-1500.0, **{"SPOT:BTCUSDT": 1000.0}), portfolio_marks={"BTCUSDT": 100, "SPOT:BTCUSDT": 100})).codes()
    assert RC.POSITION_MISMATCH in RC.reconcile(views(portfolio=pstate(BTCUSDT=1000.0, **{"SPOT:BTCUSDT": 1000.0}))).codes()
    rep = RC.reconcile(views(portfolio=pstate(**{"SPOT:BTCUSDT": 1000.0})))
    assert any(f.code == RC.POSITION_MISMATCH and f.key == "BTCUSDT" and "portfolio" in f.views for f in rep.findings)
    rep = RC.reconcile(views(portfolio=pstate(BTCUSDT=-1000.0, ETHUSDT=-500.0, **{"SPOT:BTCUSDT": 1000.0})))
    assert any(f.code == RC.MISSING_IN_VENUE and f.key == "ETHUSDT" for f in rep.findings)


def test_net_pnl_must_be_one_number_and_unknown_is_not_zero():
    assert RC.reconcile(views(net_pnl={"ledger": 2.1, "artifact": 2.1000000001})).ok
    rep = RC.reconcile(views(net_pnl={"ledger": 2.1, "artifact": 2.2}))
    assert rep.codes() == (RC.NET_PNL_MISMATCH,) and rep.findings[0].views == ("ledger", "artifact")
    assert RC.NET_PNL_MISMATCH in RC.reconcile(views(net_pnl={"ledger": None, "artifact": 0.0})).codes()
    assert RC.reconcile(views(net_pnl={"ledger": None, "artifact": None})).ok


def test_ledger_integrity_flags_are_findings():
    rep = RC.reconcile(views(ledger_flags=("FUNDING_CONFLICT:BTCUSDT:1500", "ORPHAN_FILL:G:L")))
    assert rep.codes() == (RC.LEDGER_INTEGRITY,) and len(rep.findings) == 2


@pytest.mark.parametrize("ts,now,why", [(None, NOW, "TS_UNKNOWN"), (NOW - 61_000, NOW, "AGE_"), (NOW + 1, NOW, "TS_IN_FUTURE"), (NOW, None, "NOW_UNKNOWN"), (True, NOW, "TS_UNKNOWN")])
def test_stale_unknown_or_future_views_are_findings(ts, now, why):
    rep = RC.reconcile(views(view_ts_ms={"account": ts}, now_ms=now))
    assert rep.codes() == (RC.STALE_VIEW,) and why in rep.findings[0].detail and rep.halt_new_exposure


def test_fresh_view_and_per_view_limits():
    assert RC.reconcile(views(view_ts_ms={"account": NOW - 60_000, "market": NOW - 100}, now_ms=NOW)).ok
    assert RC.STALE_VIEW in RC.reconcile(views(view_ts_ms={"market": NOW - 1_000}, now_ms=NOW), max_age_ms={"market": 500}).codes()


RES = {"registered": {"g1": [{"leg_id": "p", "symbol": "BTCUSDT", "category": "linear"}, {"leg_id": "s", "symbol": "BTCUSDT", "category": "spot"}]},
       "resolved": set(), "settled": set(), "reserved_links": {"g1-p", "g1-s"}}


def test_reservation_rules():
    assert RC.reconcile(views(reservations=RES, engine_group_states={"g1": "HEDGED"})).ok
    rep = RC.reconcile(views(reservations=RES, engine_group_states={"g1": "FAILED"}))
    assert rep.codes() == (RC.RESERVATION_LEAK,) and "FAILED" in rep.findings[0].detail
    rep = RC.reconcile(views(reservations=RES, engine_group_states={}))
    assert rep.codes() == (RC.RESERVATION_LEAK,) and "UNKNOWN_TO_ENGINE" in rep.findings[0].detail
    assert RC.reconcile(views(reservations=RES, engine_group_states={}), require_engine_knows_reserved=False).ok
    rep = RC.reconcile(views(reservations={**RES, "reserved_links": {"g1-p", "g9-p"}}, engine_group_states={"g1": "HEDGED"}))
    assert [(f.code, f.key) for f in rep.findings] == [(RC.RESERVATION_LEAK, "g9-p")]
    rep = RC.reconcile(views(reservations={**RES, "resolved": {"g1"}}, engine_group_states={"g1": "HEDGED"}))
    assert rep.codes() == (RC.RESERVATION_RELEASED_EARLY,)
    assert RC.reconcile(views(reservations={**RES, "resolved": {"g1"}}, engine_group_states={"g1": "CLOSED"})).ok


def test_halt_hook_halts_names_the_disagreement_and_never_resumes_on_its_own():
    calls = []
    h = RC.HaltOnDisagreement(lambda r, t: calls.append(("halt", r, t)), lambda r, t: calls.append(("resume", r, t)))
    bad = RC.reconcile(views(venue_positions={"linear:BTCUSDT": -12.0, "spot:BTCUSDT": 10.0}))
    assert h(bad, 5) is bad
    assert len(calls) == 1 and calls[0][0] == "halt" and calls[0][1].startswith("RECONCILE_DISAGREEMENT:") and "QTY_MISMATCH:BTCUSDT" in calls[0][1]
    h(bad, 6)                                                                                  # same findings: no second halt
    assert len(calls) == 1
    h(RC.reconcile(views()), 7)                                                                # clean report does NOT resume by itself
    assert len(calls) == 1 and h.active is not None
    assert h.clear(8) is True and calls[-1][0] == "resume" and h.active is None and h.clear(9) is False


def test_halt_hook_replaces_the_reason_when_the_finding_set_changes_and_auto_resume_is_opt_in():
    calls = []
    h = RC.HaltOnDisagreement(lambda r, t: calls.append(("halt", r)), lambda r, t: calls.append(("resume", r)), auto_resume=True)
    h(RC.reconcile(views(venue_complete=False)), 1)
    h(RC.reconcile(views(ledger_positions={"SPOT:BTCUSDT": D(10)})), 2)
    assert [c[0] for c in calls] == ["halt", "halt", "resume"] and calls[2][1] == calls[0][1]
    h(RC.reconcile(views()), 3)
    assert calls[-1][0] == "resume" and calls[-1][1] == calls[1][1] and h.active is None


def test_halt_hook_without_a_resume_callable_still_halts():
    calls = []
    h = RC.HaltOnDisagreement(lambda r, t: calls.append(r))
    h(RC.reconcile(views(venue_positions=None)), 1)
    assert len(calls) == 1 and h.clear(2) is False
