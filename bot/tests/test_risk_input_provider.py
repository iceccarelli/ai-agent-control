"""RiskInputProvider: every State field from a fake venue with a hand-computed expectation; every unknown path is None (never 0); positive control and mutation check."""
from __future__ import annotations

import dataclasses
import os
import sys
from types import SimpleNamespace

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, BOT)

import risk_input_provider as RIP  # noqa: E402
import risk_policy as RP  # noqa: E402

NOW = 1_000_000.0
POLICY = RP.Policy(per_trade_loss_usd=2.0, daily_loss_usd=3.0, strategy_drawdown_usd=5.0, portfolio_drawdown_usd=8.0, max_hold_hours=360.0, max_exposure_usd=100.0,
                   max_leverage=3.0, min_liquidity_usd=1000.0, max_unhedged_usd=5.0, min_margin_multiple=2.0, min_liquidation_distance_pct=20.0, max_data_age_s=90.0,
                   max_correlated_exposure_usd=100.0, min_recent_funding_bps=0.2, approved_by_owner=True, approval_note="test fixture, not an owner approval")


class FakeBroker:
    """Stands in for CarryBroker's reads. Any attribute set to an Exception instance raises it."""

    def __init__(self, **kw):
        self.perp_net = -0.001
        self.spot = 0.001
        self.margin = 5.0
        self.view = {"size": 0.001, "side": "Sell", "mark": 60000.0, "liq_price": 96000.0, "distance_pct": 60.0, "reason": ""}
        self.book = {"bid": 59990.0, "ask": 60010.0, "bids": [(59990.0, 1.0), (59980.0, 1.0)], "asks": [(60010.0, 0.5)], "venue_ts_ms": int((NOW - 2) * 1000)}
        self.orders = []
        self.__dict__.update(kw)

    def _get(self, name):
        v = getattr(self, name)
        if isinstance(v, Exception):
            raise v
        return v

    def get_perp_net_qty(self, symbol):
        return self._get("perp_net")

    def get_spot_inventory(self, symbol):
        return self._get("spot")

    def get_margin_multiple(self, symbol):
        return self._get("margin")

    def get_liquidation_view(self, symbol):
        return self._get("view")

    def get_book_depth(self, symbol, product, limit=None, timeout_s=None):
        return self._get("book")

    def get_open_carry_orders(self, spot_symbol, perp_symbol):
        return self._get("orders")


def snap(age=10.0, mark=60000.0):
    return SimpleNamespace(perp_mark=mark, observed_at_s=NOW - age)


def make(broker=None, **kw):
    args = dict(broker=broker or FakeBroker(), perp_symbol="BTCUSDT", spot_symbol="BTCUSDT", snapshot_getter=lambda: snap(),
                opened_ms_getter=lambda: int((NOW - 36000) * 1000), halted_getter=lambda: False,
                ledger_reads={"trade_loss_usd": lambda: 0.1, "loss_today_usd": lambda: 0.2, "strategy_drawdown_usd": lambda: 0.5, "portfolio_drawdown_usd": lambda: 0.6},
                funding_getter=lambda: (0.5, int((NOW - 3600) * 1000)),
                correlation_set={"btc_perp": lambda: 60.0, "eth_perp": lambda: 10.0},
                equity_reader=lambda: 120.0, now_s=lambda: NOW)
    args.update(kw)
    return RIP.RiskInputProvider(**args)


def test_every_field_from_a_fake_venue_matches_hand_computation():
    st = make().state()
    assert st == RP.State(
        trade_loss_usd=0.1, loss_today_usd=0.2, strategy_drawdown_usd=0.5, portfolio_drawdown_usd=0.6,
        hold_hours=10.0,                      # (1_000_000 - 964_000 s) / 3600
        exposure_usd=pytest.approx(60.0),     # |-0.001| x 60000
        leverage=pytest.approx(0.5),          # 60 / 120
        liquidity_usd=pytest.approx(30005.0),  # asks 60010 x 0.5 < bids 119970
        unhedged_usd=pytest.approx(0.0),      # |-0.001 + 0.001| x 60000
        margin_multiple=5.0, liquidation_distance_pct=60.0,
        data_age_s=10.0,                      # NOW - observed
        correlated_exposure_usd=70.0,         # 60 + 10
        recent_mean_funding_bps=0.5, operational_fault=False)


def test_unhedged_uses_signed_perp_plus_spot_from_the_venue():
    st = make(FakeBroker(spot=0.0009)).state()
    assert st.unhedged_usd == pytest.approx(6.0)          # |-0.001 + 0.0009| x 60000
    assert make(FakeBroker(perp_net=0.001, spot=0.001)).state().unhedged_usd == pytest.approx(120.0)     # a long perp doubles, not hedges


def test_flat_venue_is_measured_zero_exposure_but_margin_and_liquidation_are_unknown():
    p = make(FakeBroker(perp_net=0.0, spot=0.0))
    st = p.state()
    assert st.exposure_usd == 0.0 and st.hold_hours == 0.0 and st.unhedged_usd == 0.0
    assert st.margin_multiple is None and st.liquidation_distance_pct is None
    assert "FLAT" in p.unknown_reasons["margin_multiple"]


def test_explain_has_source_age_and_why_for_every_field():
    p = make(correlation_set=None)
    p.state()
    ex = p.explain()
    assert set(ex) == set(RIP.FIELDS)
    for d in ex.values():
        assert set(d) == {"value", "source", "age_s", "why_unknown"}
    assert ex["exposure_usd"]["value"] == pytest.approx(60.0) and ex["exposure_usd"]["why_unknown"] == "" and ex["exposure_usd"]["age_s"] == 10.0
    assert ex["correlated_exposure_usd"]["value"] is None and "no declared correlation set" in ex["correlated_exposure_usd"]["why_unknown"]
    assert p.unknown_reasons == {"correlated_exposure_usd": ex["correlated_exposure_usd"]["why_unknown"]}


# ---- unknown paths: None, never 0 -------------------------------------------------------------------------------------------------------------------------

CASES = [
    # (label, provider kwargs / broker kwargs, fields that must be None, reason substring)
    ("perp read raises", dict(b=dict(perp_net=RuntimeError("503"))), ["exposure_usd", "unhedged_usd", "leverage", "hold_hours", "margin_multiple", "liquidation_distance_pct"], "RuntimeError"),
    ("spot read raises", dict(b=dict(spot=RuntimeError("down"))), ["unhedged_usd"], "spot inventory"),
    ("margin read raises", dict(b=dict(margin=RuntimeError("x"))), ["margin_multiple"], "margin_multiple"),
    ("liquidation view raises", dict(b=dict(view=RuntimeError("x"))), ["liquidation_distance_pct"], "liquidation_view"),
    ("liq view no distance", dict(b=dict(view={"size": 0.001, "distance_pct": None, "reason": "BEYOND_VENUE_PRICE_BOUNDS"})), ["liquidation_distance_pct"], "BEYOND_VENUE_PRICE_BOUNDS"),
    ("liq view disagrees with position", dict(b=dict(view={"size": 0.5, "distance_pct": 60.0})), ["liquidation_distance_pct"], "disagrees"),
    ("liq view not a mapping", dict(b=dict(view="ok")), ["liquidation_distance_pct"], "not a mapping"),
    ("margin nan", dict(b=dict(margin=float("nan"))), ["margin_multiple"], "margin_multiple"),
    ("margin zero", dict(b=dict(margin=0.0)), ["margin_multiple"], "margin_multiple"),
    ("book raises", dict(b=dict(book=RuntimeError("x"))), ["liquidity_usd"], "orderbook"),
    ("book stale", dict(b=dict(book={"bids": [(1.0, 1.0)], "asks": [(2.0, 1.0)], "venue_ts_ms": int((NOW - 60) * 1000)})), ["liquidity_usd"], "stale"),
    ("book empty side", dict(b=dict(book={"bids": [], "asks": [(2.0, 1.0)], "venue_ts_ms": None})), ["liquidity_usd"], "empty side"),
    ("open orders unreadable", dict(b=dict(orders=RuntimeError("x"))), ["operational_fault"], "open orders"),
    ("snapshot stale", dict(p=dict(snapshot_getter=lambda: snap(age=300.0))), ["exposure_usd", "unhedged_usd", "leverage"], "stale"),
    ("snapshot missing", dict(p=dict(snapshot_getter=lambda: None)), ["exposure_usd", "unhedged_usd", "leverage", "data_age_s"], "snapshot"),
    ("snapshot getter raises", dict(p=dict(snapshot_getter=lambda: 1 / 0)), ["exposure_usd", "data_age_s"], "ZeroDivisionError"),
    ("snapshot in the future", dict(p=dict(snapshot_getter=lambda: snap(age=-100.0))), ["exposure_usd", "data_age_s"], "future"),
    ("snapshot no mark", dict(p=dict(snapshot_getter=lambda: snap(mark=None))), ["exposure_usd"], "perp_mark"),
    ("snapshot zero mark", dict(p=dict(snapshot_getter=lambda: snap(mark=0.0))), ["exposure_usd"], "perp_mark"),
    ("equity raises", dict(p=dict(equity_reader=lambda: 1 / 0)), ["leverage"], "equity"),
    ("equity zero", dict(p=dict(equity_reader=lambda: 0.0)), ["leverage"], "equity"),
    ("opened_ms absent", dict(p=dict(opened_ms_getter=lambda: None)), ["hold_hours"], "opened_ms"),
    ("opened_ms zero", dict(p=dict(opened_ms_getter=lambda: 0)), ["hold_hours"], "opened_ms"),
    ("opened_ms raises", dict(p=dict(opened_ms_getter=lambda: 1 / 0)), ["hold_hours"], "opened_ms"),
    ("opened_ms not wired", dict(p=dict(opened_ms_getter=None)), ["hold_hours"], "opened_ms"),
    ("ledger read raises", dict(p=dict(ledger_reads={"trade_loss_usd": lambda: 1 / 0})), ["trade_loss_usd"], "trade_loss_usd"),
    ("ledger value negative", dict(p=dict(ledger_reads={"trade_loss_usd": lambda: -1.0})), ["trade_loss_usd"], "trade_loss_usd"),
    ("ledger value nan", dict(p=dict(ledger_reads={"trade_loss_usd": lambda: float("nan")})), ["trade_loss_usd"], "trade_loss_usd"),
    ("funding stale", dict(p=dict(funding_getter=lambda: (0.5, int((NOW - 86400) * 1000)))), ["recent_mean_funding_bps"], "stale"),
    ("funding not wired", dict(p=dict(funding_getter=None)), ["recent_mean_funding_bps"], "funding"),
    ("funding raises", dict(p=dict(funding_getter=lambda: 1 / 0)), ["recent_mean_funding_bps"], "funding"),
    ("funding malformed", dict(p=dict(funding_getter=lambda: "0.5")), ["recent_mean_funding_bps"], "funding"),
    ("correlation member unknown", dict(p=dict(correlation_set={"a": lambda: 1.0, "b": lambda: None})), ["correlated_exposure_usd"], "b"),
    ("correlation member raises", dict(p=dict(correlation_set={"a": lambda: 1 / 0})), ["correlated_exposure_usd"], "a"),
    ("no correlation set", dict(p=dict(correlation_set=None)), ["correlated_exposure_usd"], "no declared correlation set"),
    ("halted not stated", dict(p=dict(halted_getter=lambda: None)), ["operational_fault"], "halted"),
    ("halted not wired", dict(p=dict(halted_getter=None)), ["operational_fault"], "halted"),
]


@pytest.mark.parametrize("label,cfg,none_fields,why", CASES, ids=[c[0] for c in CASES])
def test_unknown_paths_are_none_never_zero_and_say_why(label, cfg, none_fields, why):
    p = make(FakeBroker(**cfg.get("b", {})), **cfg.get("p", {}))
    st = p.state()                                   # never raises
    for f in none_fields:
        assert getattr(st, f) is None, (label, f)
        assert why.lower() in p.unknown_reasons[f].lower(), (label, f, p.unknown_reasons[f])
        d = p.explain()[f]
        assert d["value"] is None and d["why_unknown"]
    assert RP.evaluate(POLICY, st).status != RP.ALLOW


def test_halted_book_is_a_fault_true_not_unknown():
    assert make(halted_getter=lambda: True).state().operational_fault is True


def test_state_never_raises_even_if_measurement_itself_blows_up():
    p = make()
    p._measure = lambda: 1 / 0                      # type: ignore[assignment]
    st = p.state()
    assert all(getattr(st, f) is None for f in RIP.FIELDS)
    assert "PROVIDER_ERROR" in p.unknown_reasons["exposure_usd"]


def test_stale_snapshot_keeps_data_age_as_the_honest_number():
    st = make(snapshot_getter=lambda: snap(age=300.0)).state()
    assert st.data_age_s == 300.0 and st.exposure_usd is None      # policy sees the age and breaches; the mark-derived fields are unknown


# ---- positive control and mutation check ----------------------------------------------------------------------------------------------------------------------

def _measured_state():
    """A fully measured state that passes the policy: held position with venue margin and liquidation view."""
    return make().state()


def test_positive_control_fully_measured_state_passes_policy_and_one_unmeasured_field_refuses():
    st = _measured_state()
    assert RP.evaluate(POLICY, st) == RP.Decision(RP.ALLOW, ())
    for f in RIP.FIELDS:
        d = RP.evaluate(POLICY, dataclasses.replace(st, **{f: None}))
        assert d.status != RP.ALLOW and f"MISSING_INPUT:{f}" in d.reasons


def test_breach_is_visible_through_the_provider_not_hidden():
    d = RP.evaluate(POLICY, make(FakeBroker(spot=0.0)).state())      # naked short: unhedged 60 > 5
    assert "BREACH:max_unhedged_usd" in d.reasons


def test_mutation_substituting_zero_for_unknown_is_caught(monkeypatch):
    """If an unknown were defaulted to 0 the policy would ALLOW a state it cannot know to be safe. This test fails loudly if the provider ever does that."""
    p = make(FakeBroker(margin=RuntimeError("x"), view=RuntimeError("x"), book=RuntimeError("x")), correlation_set=None, funding_getter=None)
    st = p.state()
    assert RP.evaluate(POLICY, st).status != RP.ALLOW
    zeroed = dataclasses.replace(st, **{f: 0.0 for f in ("margin_multiple", "liquidation_distance_pct", "liquidity_usd", "correlated_exposure_usd", "recent_mean_funding_bps")})
    # the mutant state: zeros for min-limits breach, and zeros are what the policy would otherwise have had to treat as measured
    assert all(getattr(st, f) is None for f in ("margin_multiple", "liquidation_distance_pct", "liquidity_usd", "correlated_exposure_usd", "recent_mean_funding_bps"))
    assert all(getattr(zeroed, f) == 0.0 for f in ("margin_multiple", "correlated_exposure_usd"))
    # a zero for a MAX-limited unknown (correlated exposure) would have PASSED: that is exactly the hole the None closes
    assert "BREACH:max_correlated_exposure_usd" not in RP.evaluate(POLICY, zeroed).reasons
    assert "MISSING_INPUT:correlated_exposure_usd" in RP.evaluate(POLICY, st).reasons


def test_provider_source_has_no_zero_default_for_unknowns():
    """Static mutation guard: the provider must not use `or 0`, `, 0)` defaults or `.get(..., 0)` style fallbacks on venue reads."""
    import re
    src = open(os.path.join(BOT, "risk_input_provider.py"), encoding="utf-8").read()
    code = "\n".join(l.split("#")[0] for l in src.splitlines())
    assert not re.search(r"\bor 0(\.0)?\b", code)
    assert not re.search(r"\.get\([^)]*,\s*0(\.0)?\)", code)


def test_only_reads_the_broker_never_an_order_method():
    calls = []

    class Spy(FakeBroker):
        def __getattr__(self, name):
            calls.append(name)
            raise AttributeError(name)

    make(Spy()).state()
    assert not [c for c in calls if "place" in c or "submit" in c or "cancel" in c]
