"""Contract tests for the single equity-space sizing path.

Fully offline. The properties defended here are the ones the legacy module
violated in nine different ways at once:

* size follows from risk, not from balance times leverage times multipliers
* a negative edge sizes to ZERO — there is no floor
* a sub-minimum size is SKIPPED, never enlarged
* nothing is fabricated: no fallback balance, price, or instrument filters
"""
from __future__ import annotations

import ast
import os
import sys
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import position_sizing as ps  # noqa: E402
from persistence import StateStore, TradeRecord, utc_now_epoch  # noqa: E402
from risk_management import BillionaireRiskManager, equity_risk_fraction  # noqa: E402

EQUITY = 10_000.0
PRICE = 50_000.0

BTC_FILTERS = ps.InstrumentFilters(
    symbol="BTCUSDT",
    tick_size=Decimal("0.01"),
    qty_step=Decimal("0.000001"),
    min_qty=Decimal("0.000048"),
    min_notional=Decimal("5"),
    from_exchange=True,
)

#: ADA really does step by 0.1 — the legacy hardcoded 0.001 for every symbol.
ADA_FILTERS = ps.InstrumentFilters(
    symbol="ADAUSDT",
    tick_size=Decimal("0.0001"),
    qty_step=Decimal("0.1"),
    min_qty=Decimal("1"),
    min_notional=Decimal("5"),
    from_exchange=True,
)


@pytest.fixture()
def store(tmp_path):
    s = StateStore(str(tmp_path / "s.db"))
    s.update_equity(EQUITY)
    yield s
    s.close()


@pytest.fixture()
def rm(store):
    return BillionaireRiskManager(store=store)


@pytest.fixture()
def sizer(rm):
    return ps.BillionairePositionSizing(risk_manager=rm)


def request_for(**over):
    base = dict(
        symbol="BTCUSDT",
        side="Buy",
        current_price=PRICE,
        current_portfolio_value=EQUITY,
        stop_loss_distance=0.02,
        filters=BTC_FILTERS,
    )
    base.update(over)
    return ps.SizingRequest(**base)


def add_trades(store, n, ret):
    """n closed trades each returning `ret` as a fraction of notional."""
    for _ in range(n):
        notional = 1_000.0
        store.record_trade(TradeRecord(
            symbol="BTCUSDT", side="Buy", qty=notional / PRICE,
            entry_price=PRICE, exit_price=PRICE * (1 + ret),
            gross_pnl=notional * ret, entry_fee=0.0, exit_fee=0.0,
            opened_epoch=utc_now_epoch() - 60, closed_epoch=utc_now_epoch(),
        ))


# ---------------------------------------------------------------------------
# size follows from risk
# ---------------------------------------------------------------------------


class TestRiskBasedSizing:
    """Two limits act on every trade and the tighter one wins:

        qty_risk = equity * risk_per_trade / (price * stop_fraction)
        qty_cap  = equity * max_position_pct / price

    Risk binds only when ``stop_fraction > risk_per_trade / max_position_pct``.
    With the shipped defaults (0.005 / 0.02) that crossover is a **25% stop**, so
    for every realistic stop the notional cap binds first and actual risk per
    trade is far below the budget. That is conservative and intentional, but it
    means these tests must exercise both regimes explicitly — otherwise a broken
    risk calculation would hide behind the cap and still pass.
    """

    def test_the_two_limits_cross_where_expected(self, sizer):
        crossover = sizer.risk_per_trade_pct / sizer.max_position_size_pct
        assert crossover == pytest.approx(0.25)

    def test_a_wider_stop_gives_a_smaller_position_when_risk_binds(self, sizer):
        """The defining property of risk-based sizing, tested above the crossover."""
        tight = sizer.calculate_position_size(request_for(stop_loss_distance=0.30))
        wide = sizer.calculate_position_size(request_for(stop_loss_distance=0.60))
        assert tight.should_trade and wide.should_trade
        assert tight.qty > wide.qty
        assert wide.qty == pytest.approx(tight.qty / 2, rel=1e-3)

    def test_below_the_crossover_the_notional_cap_binds(self, sizer):
        """And when the cap binds, size is stop-independent by construction."""
        a = sizer.calculate_position_size(request_for(stop_loss_distance=0.01))
        b = sizer.calculate_position_size(request_for(stop_loss_distance=0.04))
        assert a.qty == pytest.approx(b.qty)
        assert a.notional == pytest.approx(EQUITY * sizer.max_position_size_pct)
        # ...and the realised risk is then strictly below the budget.
        assert a.risk_fraction < sizer.risk_per_trade_pct
        assert b.risk_fraction < sizer.risk_per_trade_pct

    def test_realised_risk_never_exceeds_the_budget(self, sizer):
        for stop in (0.005, 0.01, 0.02, 0.05, 0.10, 0.25, 0.50, 0.90):
            result = sizer.calculate_position_size(
                request_for(stop_loss_distance=stop)
            )
            if not result.should_trade:
                continue
            assert result.risk_fraction <= sizer.risk_per_trade_pct + 1e-9, (
                f"stop={stop} produced risk {result.risk_fraction}"
            )

    def test_notional_never_exceeds_the_position_cap(self, sizer):
        result = sizer.calculate_position_size(request_for(stop_loss_distance=0.001))
        assert result.notional <= EQUITY * sizer.max_position_size_pct + 1e-6

    def test_caller_may_request_less_risk_but_never_more(self, sizer):
        """Tested above the crossover so the risk budget is the binding limit."""
        wide = dict(stop_loss_distance=0.30)
        less = sizer.calculate_position_size(
            request_for(max_position_risk=sizer.risk_per_trade_pct / 10, **wide)
        )
        more = sizer.calculate_position_size(
            request_for(max_position_risk=0.99, **wide)
        )
        baseline = sizer.calculate_position_size(request_for(**wide))
        assert less.qty < baseline.qty
        assert more.qty == pytest.approx(baseline.qty), (
            "a caller asking for 99% risk must not get more than configured"
        )

    def test_size_scales_with_equity(self, sizer):
        small = sizer.calculate_position_size(
            request_for(current_portfolio_value=1_000.0)
        )
        large = sizer.calculate_position_size(
            request_for(current_portfolio_value=100_000.0)
        )
        assert large.qty > small.qty


# ---------------------------------------------------------------------------
# Kelly
# ---------------------------------------------------------------------------


class TestKelly:
    def test_negative_edge_sizes_to_zero_with_no_floor(self):
        """The legacy `max(0.05, ...)` kept betting 5% on a proven losing edge."""
        f, reason = ps.kelly_fraction_from_returns([-0.02] * 60, cap=0.01)
        assert f == 0.0
        assert reason == "NEGATIVE_EDGE"

    def test_no_history_does_not_produce_a_position_from_nothing(self):
        """Legacy returned 0.10 — a 10% position — with zero trades."""
        f, reason = ps.kelly_fraction_from_returns([], cap=0.01)
        assert f == 0.01
        assert "BASE_RISK" in reason

    def test_below_sample_floor_kelly_is_not_consulted(self):
        f, reason = ps.kelly_fraction_from_returns([0.5] * 5, cap=0.01)
        assert f == 0.01, "5 wildly profitable trades must not raise the risk budget"
        assert "BASE_RISK" in reason

    def test_kelly_can_only_shrink_the_configured_cap(self):
        """A hot streak must not let the strategy talk the cap upward."""
        f, _ = ps.kelly_fraction_from_returns([0.10] * 100, cap=0.005)
        assert f <= 0.005

    def test_kelly_shrinks_risk_on_a_marginal_edge(self):
        strong, _ = ps.kelly_fraction_from_returns([0.05, -0.01] * 50, cap=0.30)
        weak, _ = ps.kelly_fraction_from_returns([0.01, -0.009] * 50, cap=0.30)
        assert weak < strong

    def test_kelly_log_opt_returns_zero_below_sample_floor(self):
        assert ps.kelly_log_opt([0.05] * 10, min_samples=30) == 0.0

    def test_kelly_log_opt_returns_zero_on_all_zero_returns(self):
        assert ps.kelly_log_opt([0.0] * 200) == 0.0

    def test_kelly_log_opt_is_bounded_by_its_hard_cap(self):
        f = ps.kelly_log_opt([0.5] * 300, hard_cap=0.10, n_boot=50)
        assert 0.0 <= f <= 0.10

    def test_losing_history_blocks_sizing_entirely(self, sizer, store):
        add_trades(store, 40, -0.02)
        result = sizer.calculate_position_size(request_for())
        assert result.should_trade is False
        assert result.reason == "NO_RISK_BUDGET"

    def test_kelly_uses_fee_inclusive_returns(self, store, rm):
        """A gross-profitable but fee-negative history must read as a losing edge."""
        for _ in range(40):
            store.record_trade(TradeRecord(
                symbol="BTCUSDT", side="Buy", qty=0.02,
                entry_price=PRICE, exit_price=PRICE * 1.0001,
                gross_pnl=0.10, entry_fee=1.0, exit_fee=1.0,
                opened_epoch=utc_now_epoch() - 60, closed_epoch=utc_now_epoch(),
            ))
        assert all(r < 0 for r in store.net_returns())
        s = ps.BillionairePositionSizing(risk_manager=rm)
        assert s.calculate_position_size(request_for()).should_trade is False


# ---------------------------------------------------------------------------
# never bump up
# ---------------------------------------------------------------------------


class TestNeverBumpUp:
    def test_sub_minimum_size_is_skipped_not_enlarged(self, sizer):
        result = sizer.calculate_position_size(
            request_for(current_portfolio_value=10.0)
        )
        assert result.qty == 0.0
        assert result.reason in {"BELOW_MIN_QTY", "BELOW_MIN_NOTIONAL",
                                 "BELOW_QTY_STEP", "ZERO_QTY_FROM_RISK"}

    def test_below_min_notional_returns_zero(self):
        qty, reason = ps.clamp_to_exchange("BTCUSDT", PRICE, 0.00005, BTC_FILTERS)
        assert qty == 0.0
        assert reason in {"BELOW_MIN_QTY", "BELOW_MIN_NOTIONAL"}

    def test_quantity_snaps_down_never_up(self):
        """ADA steps by 0.1: 15.97 must become 15.9, not 16.0."""
        assert ps.snap_qty_down(15.97, Decimal("0.1")) == Decimal("15.9")
        assert ps.snap_qty_down(15.99999, Decimal("0.1")) == Decimal("15.9")

    def test_conformed_quantity_is_never_larger_than_requested(self, sizer):
        for equity in (50.0, 500.0, 5_000.0, 50_000.0):
            req = request_for(current_portfolio_value=equity, filters=ADA_FILTERS,
                              symbol="ADAUSDT", current_price=0.45)
            result = sizer.calculate_position_size(req)
            if result.should_trade:
                assert result.risk_fraction <= sizer.risk_per_trade_pct + 1e-9

    def test_real_per_symbol_steps_are_respected(self, sizer):
        """The legacy hardcoded step 0.001 for every symbol; ADA steps by 0.1."""
        result = sizer.calculate_position_size(
            request_for(symbol="ADAUSDT", current_price=0.45, filters=ADA_FILTERS)
        )
        if result.should_trade:
            assert (Decimal(str(result.qty)) % Decimal("0.1")) == 0

    def test_no_ceil_to_step_in_source(self):
        """ROUND_UP may appear only in price snapping, never for quantities."""
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "position_sizing.py",
        )
        tree = ast.parse(open(path, encoding="utf-8").read())
        for fn in ast.walk(tree):
            if isinstance(fn, ast.FunctionDef) and fn.name == "snap_qty_down":
                names = [n.id for n in ast.walk(fn) if isinstance(n, ast.Name)]
                assert "ROUND_UP" not in names


class TestPriceSnapping:
    def test_buy_entry_rounds_down_sell_entry_rounds_up(self):
        tick = Decimal("0.01")
        assert ps.snap_price(100.567, tick, side="Buy") == Decimal("100.56")
        assert ps.snap_price(100.561, tick, side="Sell") == Decimal("100.57")

    def test_stop_rounds_conservatively_the_other_way(self):
        """A Buy's stop rounds UP: exit a touch sooner, never later."""
        tick = Decimal("0.01")
        assert ps.snap_price(100.561, tick, side="Buy", is_stop=True) == Decimal("100.57")
        assert ps.snap_price(100.567, tick, side="Sell", is_stop=True) == Decimal("100.56")

    def test_unknown_side_raises(self):
        with pytest.raises(ValueError):
            ps.snap_price(100.0, Decimal("0.01"), side="Sideways")


# ---------------------------------------------------------------------------
# no fabricated inputs
# ---------------------------------------------------------------------------


class TestNoFabricatedInputs:
    @pytest.mark.parametrize(
        "override,expected",
        [
            (dict(current_price=0.0), "NO_PRICE"),
            (dict(current_price=-1.0), "NO_PRICE"),
            (dict(current_portfolio_value=0.0), "NO_EQUITY"),
            (dict(stop_loss_distance=0.0), "NO_STOP_DISTANCE"),
            (dict(stop_loss_distance=-0.01), "NO_STOP_DISTANCE"),
            (dict(stop_loss_distance=2.0), "STOP_DISTANCE_NOT_A_FRACTION"),
            (dict(filters=None), "NO_INSTRUMENT_FILTERS"),
            (dict(symbol=""), "NO_SYMBOL"),
        ],
    )
    def test_missing_or_bad_input_means_no_trade(self, sizer, override, expected):
        result = sizer.calculate_position_size(request_for(**override))
        assert result.qty == 0.0
        assert result.reason == expected

    def test_instrument_filters_cannot_be_defaulted(self):
        """Audit C14: a hardcoded filter set is fabricated exchange data."""
        with pytest.raises(ValueError):
            ps.InstrumentFilters.parse("BTCUSDT", {"tickSize": "0.01"})

    def test_filters_record_their_provenance(self):
        f = ps.InstrumentFilters.parse(
            "BTCUSDT",
            {"tickSize": "0.01", "qtyStep": "0.000001",
             "minOrderQty": "0.000048", "minOrderAmt": "5"},
            from_exchange=True,
        )
        assert f.from_exchange is True

    def test_no_fallback_balance_constants_in_source(self):
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "position_sizing.py",
        )
        src = open(path, encoding="utf-8").read()
        for ln in src.splitlines():
            if ln.lstrip().startswith("#") or "``" in ln:
                continue
            assert "10000.0" not in ln and "1000.0)" not in ln, (
                f"possible fabricated balance: {ln.strip()}"
            )

    def test_no_os_getenv(self):
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "position_sizing.py",
        )
        src = open(path, encoding="utf-8").read()
        offenders = [
            ln.strip() for ln in src.splitlines()
            if ("os.getenv" in ln or "os.environ" in ln)
            and not ln.lstrip().startswith("#") and "``" not in ln
        ]
        assert offenders == []

    def test_win_rate_is_none_not_a_half_when_history_is_thin(self, rm):
        tracker = ps.get_profit_tracker(rm)
        assert tracker.win_rate() is None


# ---------------------------------------------------------------------------
# caps agree with the risk manager
# ---------------------------------------------------------------------------


class TestCapsAgreeWithRiskManager:
    def test_sized_quantity_passes_the_risk_gate(self, sizer, rm):
        """Sizing and gating must not disagree: whatever the sizer produces
        must survive the gate that runs immediately after it."""
        result = sizer.calculate_position_size(request_for(stop_loss_distance=0.02))
        assert result.should_trade
        decision = rm.gate_order(
            symbol="BTCUSDT", side="Buy", entry_price=PRICE,
            stop_loss=PRICE * 0.98, quantity=result.qty, account_equity=EQUITY,
        )
        assert decision.ok is True, f"sizer produced a size the gate rejects: {decision}"

    def test_aggregate_exposure_headroom_shrinks_the_size(self, sizer, store):
        before = sizer.calculate_position_size(request_for()).qty
        store.upsert_position("ETHUSDT", "Buy", 0.3, 3_000.0, stop_price=2_900.0)
        after = sizer.calculate_position_size(request_for()).qty
        assert after < before

    def test_full_aggregate_exposure_blocks_sizing(self, sizer, store, rm):
        store.upsert_position(
            "ETHUSDT", "Buy", 1.0,
            EQUITY * rm.max_total_exposure_pct, stop_price=1.0,
        )
        result = sizer.calculate_position_size(request_for())
        assert result.qty == 0.0
        assert result.reason in {"AGGREGATE_EXPOSURE_FULL", "CORRELATION_BUCKET_FULL"}

    def test_correlation_bucket_headroom_is_respected(self, sizer, store):
        for sym, px in (("SOLUSDT", 100.0), ("AVAXUSDT", 50.0)):
            store.upsert_position(sym, "Buy", 10.0, px, stop_price=px * 0.98)
        result = sizer.calculate_position_size(
            request_for(symbol="ADAUSDT", current_price=0.45, filters=ADA_FILTERS)
        )
        if result.should_trade:
            assert result.notional <= EQUITY * 0.30

    def test_unreadable_exposure_blocks_sizing(self, sizer):
        class Broken:
            def __getattr__(self, n):
                def boom(*a, **k):
                    raise RuntimeError("unavailable")
                return boom

        sizer.risk_manager.store = Broken()
        result = sizer.calculate_position_size(request_for())
        assert result.qty == 0.0


# ---------------------------------------------------------------------------
# structure
# ---------------------------------------------------------------------------


class TestStructure:
    def test_no_duplicate_top_level_definitions(self):
        import collections

        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "position_sizing.py",
        )
        tree = ast.parse(open(path, encoding="utf-8").read())
        names = collections.Counter(
            n.name for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        )
        assert {k: v for k, v in names.items() if v > 1} == {}

    def test_every_legacy_entry_point_routes_to_the_one_sizer(self, rm):
        """Nine sizers became one; the old names are aliases, not rivals."""
        args = ("BTCUSDT", "Buy", PRICE, EQUITY, 0.02)
        kw = dict(filters=BTC_FILTERS, risk_manager=rm)
        results = [
            ps.plan_quantity(*args, **kw),
            ps.compute_size(*args, **kw),
            ps.compute(*args, **kw),
            ps.size_order(*args, **kw),
            ps.eu_size_plan(*args, **kw),
            ps.kelly_like_size(*args, **kw),
        ]
        quantities = {round(r.qty, 12) for r in results}
        assert len(quantities) == 1, f"sizers disagree: {quantities}"

    def test_compatibility_surface_present(self):
        for name in ("BillionairePositionSizing", "ExchangeAwarePositionSizer",
                     "SizingRequest", "MarketRegime", "kelly_log_opt",
                     "calculate_position_size_with_kelly", "compute_size",
                     "size_order", "get_profit_tracker", "cycles",
                     "clamp_to_exchange", "kelly_like_size"):
            assert hasattr(ps, name), f"position_sizing.{name} disappeared"

    def test_sizing_result_defaults_to_no_trade(self):
        assert ps.SizingResult(symbol="X").should_trade is False
        assert bool(ps.SizingResult(symbol="X")) is False

    def test_no_aggressive_multiplier_stacking(self):
        """Legacy defaults multiplied to 562% of balance before clamping."""
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "position_sizing.py",
        )
        src = open(path, encoding="utf-8").read()
        for banned in ("AGGRESSIVE_MULTIPLIER", "CONFIDENCE_MULTIPLIER",
                       "SIGNAL_STRENGTH_MULTIPLIER"):
            code = [
                ln for ln in src.splitlines()
                if banned in ln and not ln.lstrip().startswith("#") and "``" not in ln
            ]
            assert code == [], f"{banned} still used: {code}"
