"""The carry book's evidence verdict: only reconciled cash counts, and the
owner's policy inputs are never invented."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import carry_evidence as cev                                  # noqa: E402
import config as _config                                      # noqa: E402
import strategy_evidence as se                                # noqa: E402

DAY = 86400.0
NOW = 2_000_000_000.0
WINDOW = (NOW - 400 * DAY, NOW - 200 * DAY)        # designed and tuned before this


def cycles(n, nets=(2.0, 3.0, 1.0, 4.0, 2.5)):
    """Cycles in the shape of the REAL report's ledger_detail (`end_ms`, not
    `last_ms`; the completeness flags and the cycle's own class)."""
    return [{"cycle_id": f"cyc-{i}", "start_ms": int((NOW - (150 - 3 * i) * DAY) * 1000),
             "end_ms": int((NOW - (149 - 3 * i) * DAY) * 1000),
             "net_usd": nets[i % len(nets)], "outcome": "CLOSED", "closed": True,
             "evidence_class": "ACTUAL_RECONCILED", "fees_complete": True,
             "funding_complete": True,
             # the real report always says; a cycle that does not is refused
             "overlay": False} for i in range(n)]


def report(n=25, *, cls="ACTUAL_RECONCILED", nets=(2.0, 3.0, 1.0, 4.0, 2.5)):
    cs = cycles(n, nets)
    ids = [c["cycle_id"] for c in cs] if cls == "ACTUAL_RECONCILED" else []
    return {
        "environment": {"declared": "mainnet", "actual_reconciled_eligible": True,
                        "recorded_seen": ["mainnet"],
                        "booked_figures_class": "UNAVAILABLE"},
        "invariants_hold": True, "unresolved_carry_orders": 0,
        "sections": {
            "realized_cash": {"evidence_class": cls, "realized_cash_cycle_ids": ids,
                              "realized_cash_cycles": len(ids)},
            "closed_cycles_booked": {"cycle_count": 0},
            "ledger_detail": {"cycles": cs}}}


def provider(rep, *, window=WINDOW, dd=50.0, raises=False):
    def fn(db, **kw):
        if raises:
            raise RuntimeError("ledger unreadable")
        return rep
    return cev.make_provider(db_path="x.db", environment="mainnet",
                             strategy_id="btc_funding_carry",
                             discovery_window_s=window, drawdown_limit_usd=dd,
                             report_fn=fn, now_fn=lambda: NOW)


def test_reconciled_cycles_after_the_window_with_owner_inputs_can_allocate():
    v = provider(report())()
    assert isinstance(v, se.EvidenceVerdict) and v.verdict == se.ALLOCATE
    assert v.grants_owner_approval is False
    assert v.strategy_id == "btc_funding_carry"


def test_without_the_owners_window_the_verdict_is_insufficient_never_invented():
    v = provider(report(), window=None)()
    assert v.verdict == se.INSUFFICIENT


def test_without_the_owners_drawdown_limit_the_verdict_is_insufficient():
    v = provider(report(), dd=None)()
    assert v.verdict == se.INSUFFICIENT


def test_evidence_inside_the_discovery_window_is_not_independent():
    inside = (NOW - 160 * DAY, NOW - 100 * DAY)
    v = provider(report(), window=inside)()
    assert v.verdict == se.BLOCK


@pytest.mark.parametrize("cls", ["PAPER_EXECUTION", "UNAVAILABLE",
                                 "UNDECLARED_ENVIRONMENT", "HISTORICAL_SIMULATION",
                                 "FORWARD_SIMULATION", "OPEN_MARK_TO_MARKET"])
def test_nothing_but_reconciled_cash_counts_as_a_trade(cls):
    rep = report(25, cls=cls)
    ev = cev.evidence_from_report(rep, strategy_id="s", observed_at_s=NOW,
                                  discovery_window_s=WINDOW, drawdown_limit_usd=50.0)
    assert ev.trades == ()
    assert provider(rep)().verdict == se.INSUFFICIENT


def test_losing_reconciled_cash_is_blocked():
    v = provider(report(nets=(-1.0, -2.0, 0.5)))()
    assert v.verdict == se.BLOCK


def test_too_few_reconciled_cycles_are_insufficient():
    assert provider(report(5))().verdict == se.INSUFFICIENT


def test_only_the_ids_the_report_calls_realized_cash_are_used():
    rep = report(25)
    rep["sections"]["realized_cash"]["realized_cash_cycle_ids"] = ["cyc-0"]
    rep["sections"]["realized_cash"]["realized_cash_cycles"] = 1
    ev = cev.evidence_from_report(rep, strategy_id="s", observed_at_s=NOW,
                                  discovery_window_s=WINDOW, drawdown_limit_usd=50.0)
    assert len(ev.trades) == 1


def test_a_cycle_the_report_cannot_time_is_not_used():
    rep = report(25)
    rep["sections"]["ledger_detail"]["cycles"][0]["start_ms"] = None
    ev = cev.evidence_from_report(rep, strategy_id="s", observed_at_s=NOW,
                                  discovery_window_s=WINDOW, drawdown_limit_usd=50.0)
    assert len(ev.trades) == 24


def test_an_unreadable_ledger_is_no_verdict_not_a_pass():
    assert provider(report(), raises=True)() is None


def test_the_verdict_is_recomputed_every_call():
    rep = report()
    p = provider(rep)
    assert p().verdict == se.ALLOCATE
    rep["sections"]["realized_cash"]["realized_cash_cycle_ids"] = []
    rep["sections"]["realized_cash"]["realized_cash_cycles"] = 0
    assert p().verdict == se.INSUFFICIENT


def test_a_real_testnet_ledger_yields_no_trades(tmp_path):
    """End to end over the real report builder: a database whose execution
    records are testnet cannot supply realized cash."""
    from persistence import StateStore
    import ledger as L
    path = str(tmp_path / "s.db")
    store = StateStore(path)
    store.record_execution_environment("testnet")
    store.save_ledger(L.Journal().to_rows())
    store.close()
    v = cev.make_provider(db_path=path, environment="testnet",
                          strategy_id="btc_funding_carry",
                          discovery_window_s=WINDOW, drawdown_limit_usd=50.0,
                          now_fn=lambda: NOW)()
    assert isinstance(v, se.EvidenceVerdict) and v.verdict == se.INSUFFICIENT


def test_config_policy_inputs_default_to_none_and_are_validated():
    cfg = _config.load({})
    assert cfg.CARRY_EVIDENCE_DISCOVERY_START_EPOCH is None
    assert cfg.CARRY_EVIDENCE_DISCOVERY_END_EPOCH is None
    assert cfg.CARRY_EVIDENCE_DRAWDOWN_LIMIT_USD is None
    cfg = _config.load({"CARRY_EVIDENCE_DRAWDOWN_LIMIT_USD": "75",
                        "CARRY_EVIDENCE_DISCOVERY_START_EPOCH": "1.6e9"})
    assert cfg.CARRY_EVIDENCE_DRAWDOWN_LIMIT_USD == 75.0
    for bad in ({"CARRY_EVIDENCE_DRAWDOWN_LIMIT_USD": "0"},
                {"CARRY_EVIDENCE_DRAWDOWN_LIMIT_USD": "-5"},
                {"CARRY_EVIDENCE_DISCOVERY_END_EPOCH": "nan"},
                {"CARRY_EVIDENCE_DISCOVERY_START_EPOCH": "soon"}):
        with pytest.raises(_config.ConfigError):
            _config.load(bad)


def test_main_builds_a_provider_only_for_an_on_disk_store(tmp_path):
    import main as _main
    from persistence import StateStore

    class Bot:
        pass
    cfg = _config.load({"BOOK_MODE": "carry", "CARRY_BORROW_APR": "0.0",
                        "CARRY_EXECUTION_MODE": "overlay"})
    b = Bot()
    b.store = StateStore(str(tmp_path / "s.db"))
    assert callable(_main._carry_evidence_provider(b, cfg))
    b.store.close()
    b.store = object()
    assert _main._carry_evidence_provider(b, cfg) is None


def test_an_inverted_or_partial_window_is_treated_as_unset(monkeypatch):
    import main as _main
    seen = []
    monkeypatch.setattr(cev, "make_provider", lambda **kw: seen.append(kw) or (lambda: None))

    class Bot:
        class store:
            _path = "x.db"
    for env in ({"CARRY_EVIDENCE_DISCOVERY_START_EPOCH": "2000",
                 "CARRY_EVIDENCE_DISCOVERY_END_EPOCH": "1000"},
                {"CARRY_EVIDENCE_DISCOVERY_START_EPOCH": "1000"},
                {}):
        _main._carry_evidence_provider(Bot(), _config.load(env))
    assert [kw["discovery_window_s"]() for kw in seen] == [None, None, None]
    _main._carry_evidence_provider(Bot(), _config.load(
        {"CARRY_EVIDENCE_DISCOVERY_START_EPOCH": "1000",
         "CARRY_EVIDENCE_DISCOVERY_END_EPOCH": "2000"}))
    assert seen[-1]["discovery_window_s"]() == (1000.0, 2000.0)


# -- an overlay's price leg is a hedge, not an edge ----------------------------

def overlay_report(nets, leg, n=25):
    rep = report(n, nets=nets)                 # corroborated mainnet cash
    for c in rep["sections"]["ledger_detail"]["cycles"]:
        c["overlay"] = True
        c["hedged_pair_inventory_leg"] = leg
    return rep


def test_a_short_perp_windfall_in_a_falling_market_is_not_carry_evidence():
    # Overlay short at 60000, funding +100, fees 33 each way: exit 54000 books
    # +6034, exit 60000 +34, exit 66000 -5966. The first reads as a run of wins
    # and used to reach ALLOCATE on BTC's direction alone.
    rep = overlay_report((6034.0, 6100.0, 5990.0, 6050.0, 6010.0),
                         "DERIVED_FROM_PERP_FILL_PRICES")
    ev = cev.evidence_from_report(rep, strategy_id="s", observed_at_s=NOW,
                                  discovery_window_s=WINDOW, drawdown_limit_usd=50.0)
    assert ev.trades == ()
    v = provider(rep)()
    assert v.verdict == se.INSUFFICIENT


def test_a_rally_does_not_breach_the_evidence_either():
    rep = overlay_report((-5966.0, -6000.0, -5900.0, -6100.0, -5950.0),
                         "DERIVED_FROM_PERP_FILL_PRICES")
    assert provider(rep)().verdict == se.INSUFFICIENT      # not BLOCK


def test_an_overlay_with_its_inventory_leg_booked_is_still_not_cash_evidence():
    # A booked inventory leg is a MARK of coin that was never sold. It makes
    # the pair measurable (see tests/test_cash_vs_marked_pair.py); it does not
    # make the cycle cash, and a cash-only policy must not be satisfied by it.
    rep = overlay_report((2.0, 3.0, 1.0, 4.0, 2.5), "BOOKED_MARK")
    ev = cev.evidence_from_report(rep, strategy_id="s", observed_at_s=NOW,
                                  discovery_window_s=WINDOW,
                                  drawdown_limit_usd=50.0)
    assert ev.trades == () and ev.unqualified_results == 25
    assert provider(rep)().verdict == se.INSUFFICIENT


def test_a_cycle_that_does_not_say_whether_it_is_an_overlay_is_refused():
    rep = report()
    for c in rep["sections"]["ledger_detail"]["cycles"]:
        del c["overlay"]
    assert provider(rep)().verdict == se.INSUFFICIENT


def test_an_overlay_with_no_stated_inventory_leg_is_left_out():
    rep = overlay_report((2.0, 3.0, 1.0, 4.0, 2.5), None)
    assert provider(rep)().verdict == se.INSUFFICIENT


def test_an_acquire_cycle_holds_both_legs_and_counts():
    rep = report(25)
    for c in rep["sections"]["ledger_detail"]["cycles"]:
        c["overlay"] = False
        c["hedged_pair_inventory_leg"] = "SPOT_LEG_BOOKED"
    assert provider(rep)().verdict == se.ALLOCATE
