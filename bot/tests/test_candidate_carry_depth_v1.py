"""candidate_carry_depth/v1: a separate, versioned, order-less recommender. It refuses
on stale/incomplete data, on thin economics, and on a funding forecast that has turned;
it never places an order; and it leaves the frozen strategy alone."""
from __future__ import annotations

import ast
import os

import pytest

import candidate_carry_depth_v1 as C
import md_events as E
import md_state as S
from test_md_integration import complete_state

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RICH = [6.0, 5.8, 6.1, 5.9, 6.0, 6.2]


def decide(state_mod=None, **kw):
    st, now, wall = complete_state()
    if state_mod:
        state_mod(st)
    view = st.view(now, wall)
    args = dict(mode="overlay", notional_usd=100.0, funding_history_bps=RICH, borrow_apr=0.0,
                opportunity_cost_apr=0.0, latency_allowance_bps=2.0,
                account={"available_margin_usd": 5000.0})
    args.update(kw)
    return C.decide(view, **args)


def forecast(bps):
    def mod(st):
        st.tickers[E.LINEAR].fields["fundingRate"] = bps / 1e4
    return mod


def test_it_recommends_entry_only_when_data_costs_and_forecast_all_agree():
    d = decide(forecast(6.0))
    assert d.action == C.ENTER and d.reasons == () and d.economics.net_benefit_bps > 0
    assert d.simulated and d.version == "candidate_carry_depth/v1" and len(d.params_hash) == 64


@pytest.mark.parametrize("mod,kw,needle", [
    (forecast(-0.5), {}, "FORECAST_NOT_POSITIVE"),
    (forecast(0.0), {}, "FORECAST_NOT_POSITIVE"),
    (forecast(1.0), {}, "FORECAST_BELOW_HISTORY"),                 # positive, but far under a ~6 bps history
    (forecast(6.0), {"latency_allowance_bps": None}, "latency_allowance"),
    (forecast(6.0), {"account": None}, "INCOMPLETE:available_margin"),
    (forecast(6.0), {"funding_history_bps": [6.0, 6.0]}, "TOO_FEW_SETTLED_PRINTS"),
    (forecast(6.0), {"borrow_apr": None}, "INCOMPLETE:"),
    (forecast(6.0), {"funding_history_bps": [1.0, 0.9, 1.2, 0.8, 1.1, 1.0]}, "NET_BENEFIT_WITHIN_UNCERTAINTY"),
    (forecast(6.0), {"funding_history_bps": [-6.0, -5.8, -6.1, -5.9, -6.0]}, "NET_BENEFIT_NOT_POSITIVE"),
    (forecast(6.0), {"notional_usd": 1e9}, "INCOMPLETE:"),         # more than the book displays
    (lambda st: (forecast(6.0)(st), st.specs.clear()), {}, "DATA:instruments:NO_SPEC"),
    (lambda st: (forecast(6.0)(st), st.rest_touch.clear()), {}, "DATA:rest_agreement"),
    (lambda st: (forecast(6.0)(st), st.on_disconnected(E.SPOT)), {}, "DATA:spot_connection"),
])
def test_it_refuses_with_a_named_reason(mod, kw, needle):
    d = decide(mod, **kw)
    assert d.action == C.REFUSE and any(needle in r for r in d.reasons), d.reasons


def test_it_never_claims_an_order_and_names_what_must_still_pass():
    rec = decide(forecast(6.0)).to_record()
    assert rec["order_placed"] is False and rec["simulated"] is True
    assert "strategy-evidence" in " ".join(rec["requires_before_any_order"])
    assert set(rec["classes"]) == {"OBSERVED", "CALCULATED", "ESTIMATED", "UNKNOWN"}


def test_the_parameter_hash_pins_the_declared_rule(monkeypatch):
    h = C.params_hash()
    assert h == C.params_hash()
    monkeypatch.setitem(C.PARAMS, "forecast_floor_sd_multiple", 2.0)
    assert C.params_hash() != h


def test_the_candidate_is_pure_and_independent_of_the_frozen_strategy():
    tree = ast.parse(open(os.path.join(BOT, "candidate_carry_depth_v1.py")).read())
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            mods.add((n.module or "").split(".")[0])
    assert mods <= {"__future__", "hashlib", "json", "math", "dataclasses", "typing", "carry_costs",
                    "md_economics", "md_state", "strategy_evidence"}
    assert not any("funding_carry_fade" in m for m in mods)
    assert "NULL HYPOTHESIS" in open(os.path.join(BOT, "candidate_carry_depth_v1.py")).read()


def test_a_recommendation_on_a_stale_view_is_a_refusal():
    st, now, wall = complete_state()
    view = st.view(now + 6.0, wall + 6000)
    d = C.decide(view, mode="overlay", notional_usd=100.0, funding_history_bps=RICH, borrow_apr=0.0,
                 opportunity_cost_apr=0.0, latency_allowance_bps=2.0,
                 account={"available_margin_usd": 5000.0})
    assert d.action == C.REFUSE and any(r.startswith("DATA:perp_book:STALE") for r in d.reasons)
