"""The constrained shadow pack — caps, monitors, revoke, and what stays blocked.

WHAT THIS FILE IS GUARDING
==========================
Slice 57 produced a thin research clear. Slice 58 builds the apparatus for
piloting it under glass. The failure mode of such apparatus is not that it does
not work — it is that it quietly becomes the thing it was built to constrain: a
cap that grows, a monitor that retunes, a revoke path a process can call on its
own behalf, a shadow fill that reads like a profit claim.

So the tests here are mostly about what the shadow layer **cannot** do.
"""
from __future__ import annotations

import ast
import datetime as dt
import inspect
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tools"))

import project_status as ps  # noqa: E402
import session_log as sl  # noqa: E402
import shadow  # noqa: E402
import shadow_strategy  # noqa: E402
from signals import funding_carry_fade_btc_v1 as fb  # noqa: E402

ARTIFACTS = os.path.join(REPO, "artifacts")
DAY = dt.timedelta(days=1)
BASE = dt.date(2026, 1, 1)


def trade(index, net_r, *, hold=5, notional=100.0, direction=None):
    entry = BASE + index * DAY
    exit_ = entry + hold * DAY
    return shadow.ShadowTrade(
        entry_utc=f"{entry.isoformat()}T00:00:00Z",
        exit_utc=f"{exit_.isoformat()}T00:00:00Z",
        direction=direction or fb.SHORT_SETUP,
        net_r=float(net_r), notional_usd=float(notional))


def series(values, *, spacing=7):
    return [trade(i * spacing, v) for i, v in enumerate(values)]


class Status:
    """A stand-in ProjectStatus. Only the fields the scope gate reads."""

    def __init__(self, cleared="funding_carry_fade_btc_v1", mode="paper",
                 live=False):
        self.cleared_edge_signal = cleared
        self.execution_mode = mode
        self.live_authorized = live


# ===========================================================================
# 1 — nothing was retuned, and the shadow shadows the CLEARED rule
# ===========================================================================


class TestTheShadowedRuleIsTheClearedRule:

    def test_the_constants_fingerprint_is_the_pinned_one(self):
        """If any constant moved, this shadow is piloting a different rule
        from the one that cleared — which would make the whole pilot
        meaningless while looking entirely healthy."""
        assert shadow.constants_fingerprint()[:32] == \
            shadow.CONSTANTS_FINGERPRINT

    def test_the_shadow_declares_no_constants_of_its_own(self):
        """Every number that shapes a trade must come from the signal module."""
        source = inspect.getsource(shadow_strategy)
        for name in ("FUND_ABS", "STOP_ATR", "TAKE_PROFIT_R", "HORIZON",
                     "ATR_PERIOD", "ROUND_TRIP_BPS"):
            assert f"{name} =" not in source, name
            assert f"{name}=" not in source.replace("signal_module.", ""), name

    def test_the_signal_constants_are_still_the_frozen_ones(self):
        assert fb.CONSTANTS == {
            "FUND_ABS": 0.0001, "STOP_ATR": 1.5, "TAKE_PROFIT_R": 1.0,
            "TAKE_PROFIT_ATR": 1.5, "HORIZON": 5, "ATR_PERIOD": 14,
            "LOCKUP": 1, "ROUND_TRIP_BPS": 25.0, "ENTRY_ON": "next_open"}

    def test_the_scope_is_one_signal_and_one_symbol(self):
        assert shadow.SHADOW_SIGNAL == "funding_carry_fade_btc_v1"
        assert shadow.SHADOW_SYMBOL == "BTCUSDT"


# ===========================================================================
# 2 — the scope gate
# ===========================================================================


class TestShadowIsPermittedOnlyInScope:

    def test_it_runs_for_the_cleared_signal_in_paper(self):
        allowed, why = shadow.shadow_is_permitted(Status())
        assert allowed, why

    @pytest.mark.parametrize("cleared", [None, "donchian_breakout_v1",
                                         "funding_carry_fade_v1", ""])
    def test_it_refuses_when_the_flag_is_not_this_signal(self, cleared):
        allowed, why = shadow.shadow_is_permitted(Status(cleared=cleared))
        assert not allowed
        assert "cleared_edge_signal" in why

    def test_it_refuses_outside_paper_mode(self):
        allowed, why = shadow.shadow_is_permitted(Status(mode="live"))
        assert not allowed and "paper" in why

    def test_it_refuses_beside_an_armed_shell(self):
        """Belt to the braces: the live chain does not consult this, and this
        refuses anyway."""
        allowed, why = shadow.shadow_is_permitted(Status(live=True))
        assert not allowed and "live is authorised" in why

    def test_it_refuses_a_revoked_signal(self, tmp_path):
        path = str(tmp_path / "rev.json")
        shadow.revoke_cleared_edge(
            shadow.SHADOW_SIGNAL, acknowledgement=shadow.REVOCATION_ACK,
            reason="pilot decayed", operator="a human",
            at_utc="2026-02-01T00:00:00Z", path=path)
        allowed, why = shadow.shadow_is_permitted(Status(),
                                                  revocations_path=path)
        assert not allowed and "revoked" in why


# ===========================================================================
# 3 — the hard caps
# ===========================================================================


class TestTheHardCapsAreEnforced:

    def test_the_frozen_numbers_are_the_design_notes(self):
        """EDGE.md §41d, fixed before shadow.py existed."""
        assert shadow.SHADOW_MAX_CONCURRENT_POSITIONS == 1
        assert shadow.SHADOW_MAX_ENTRIES_PER_DAY == 1
        assert shadow.SHADOW_MAX_NOTIONAL_USD == 100.00

    def test_the_notional_cap_is_a_fixed_dollar_figure(self):
        """§41d: a percentage cap grows with the account, so it authorises
        more risk the better things go — backwards for a pilot."""
        assert isinstance(shadow.SHADOW_MAX_NOTIONAL_USD, float)
        source = inspect.getsource(shadow)
        assert "equity" not in source.lower().split("max_notional_usd")[1][:400]

    def test_an_allowed_entry_passes_every_cap(self):
        caps = shadow.ShadowCaps()
        assert caps.why_blocked(symbol="BTCUSDT", day="2026-01-01",
                                notional_usd=100.0) is None

    def test_a_second_position_is_blocked(self):
        caps = shadow.ShadowCaps()
        caps.record_entry(day="2026-01-01")
        why = caps.why_blocked(symbol="BTCUSDT", day="2026-01-02",
                               notional_usd=100.0)
        assert why and "already open" in why

    def test_a_second_entry_on_the_same_day_is_blocked(self):
        caps = shadow.ShadowCaps()
        caps.record_entry(day="2026-01-01")
        caps.record_exit()
        why = caps.why_blocked(symbol="BTCUSDT", day="2026-01-01",
                               notional_usd=100.0)
        assert why and "per calendar day" in why

    def test_the_next_day_is_allowed_again(self):
        caps = shadow.ShadowCaps()
        caps.record_entry(day="2026-01-01")
        caps.record_exit()
        assert caps.why_blocked(symbol="BTCUSDT", day="2026-01-02",
                                notional_usd=100.0) is None

    @pytest.mark.parametrize("notional", [100.01, 250.0, 1_000_000.0])
    def test_oversized_notional_is_blocked(self, notional):
        caps = shadow.ShadowCaps()
        why = caps.why_blocked(symbol="BTCUSDT", day="2026-01-01",
                               notional_usd=notional)
        assert why and "exceeds the frozen cap" in why

    def test_exactly_the_cap_is_allowed(self):
        """`>` not `>=` — the cap is a ceiling, not an exclusive bound."""
        caps = shadow.ShadowCaps()
        assert caps.why_blocked(symbol="BTCUSDT", day="2026-01-01",
                                notional_usd=100.00) is None

    @pytest.mark.parametrize("notional", [0.0, -1.0])
    def test_a_non_positive_notional_is_blocked(self, notional):
        caps = shadow.ShadowCaps()
        assert caps.why_blocked(symbol="BTCUSDT", day="2026-01-01",
                                notional_usd=notional) is not None

    @pytest.mark.parametrize("symbol", ["ETHUSDT", "SOLUSDT", "BTCUSD", ""])
    def test_another_symbol_is_blocked(self, symbol):
        caps = shadow.ShadowCaps()
        why = caps.why_blocked(symbol=symbol, day="2026-01-01",
                               notional_usd=100.0)
        assert why and "only" in why

    def test_the_refusal_says_which_cap_bound(self):
        """An operator reading a shadow log should not have to guess."""
        caps = shadow.ShadowCaps()
        caps.record_entry(day="2026-01-01")
        assert isinstance(caps.why_blocked(symbol="BTCUSDT", day="2026-01-01",
                                           notional_usd=100.0), str)


# ===========================================================================
# 4 — the monitors, as pure functions against hand-built inputs
# ===========================================================================


class TestTheMonitorsAreThePreDeclaredOnes:

    def test_every_threshold_matches_the_design_note(self):
        t = shadow.MONITOR_THRESHOLDS
        assert t["M1_rolling_trades"] == {
            "k": 10, "warn_below": 0.0, "alert_below": -0.25, "min_trades": 10}
        assert t["M2_rolling_days"] == {
            "window_days": 90, "warn_below": 0.0, "alert_below": -0.25,
            "min_trades": 5}
        assert t["M3_concentration"] == {
            "top_n": 3, "warn_above": 0.60, "min_trades": 10}
        assert t["M4_halves"] == {"min_trades": 20}

    def test_the_alert_threshold_is_worse_than_this_rules_worst_fold(self):
        """§41e's derivation, asserted rather than trusted: -0.25 must sit
        beyond fold 2's -0.2124, or 'worse than anything in its own history'
        is not what the number means."""
        assert shadow.MONITOR_THRESHOLDS["M1_rolling_trades"]["alert_below"] \
            < -0.2124

    def test_the_concentration_bar_is_above_the_oos_samples_own_value(self):
        """The OOS sample sits at 0.41. A bar below that would fire on the
        evidence that produced the clear."""
        assert shadow.MONITOR_THRESHOLDS["M3_concentration"]["warn_above"] > 0.41


class TestM1RollingTrades:

    def test_insufficient_data_below_k(self):
        r = shadow.evaluate_monitors(series([0.1] * 9))[0]
        assert r.name == "M1_rolling_trades" and r.state == "INSUFFICIENT_DATA"

    def test_ok_when_positive(self):
        r = shadow.evaluate_monitors(series([0.2] * 12))[0]
        assert r.state == "OK" and r.value == pytest.approx(0.2)

    def test_warn_when_negative_but_above_alert(self):
        r = shadow.evaluate_monitors(series([-0.1] * 12))[0]
        assert r.state == "WARN"

    def test_alert_when_below_the_alert_bar(self):
        r = shadow.evaluate_monitors(series([-0.3] * 12))[0]
        assert r.state == "ALERT"

    def test_exactly_zero_is_not_a_warn(self):
        """`< 0.0`, not `<= 0.0`."""
        r = shadow.evaluate_monitors(series([0.0] * 12))[0]
        assert r.state == "OK"

    def test_it_reads_only_the_last_k(self):
        """A good year does not rescue a bad month."""
        trades = series([1.0] * 30 + [-0.5] * 10)
        assert shadow.evaluate_monitors(trades)[0].state == "ALERT"

    def test_it_is_pure(self):
        trades = series([0.2] * 12)
        assert shadow.rolling_mean_r(trades, 10) == \
            shadow.rolling_mean_r(trades, 10)


class TestM2RollingDays:

    def test_insufficient_when_too_few_in_window(self):
        trades = [trade(0, 0.5), trade(1, 0.5)]
        r = shadow.evaluate_monitors(trades, asof=BASE + 10 * DAY)[1]
        assert r.state == "INSUFFICIENT_DATA"

    def test_it_keys_on_the_exit_date_not_the_entry(self):
        """A trade is evidence once it is RESOLVED, not once it is opened."""
        value, n = shadow.rolling_mean_r_by_days(
            [trade(0, 0.5, hold=5)], 90, asof=BASE + 3 * DAY)
        assert n == 0 and value is None

    def test_trades_outside_the_window_are_excluded(self):
        old = series([1.0] * 6, spacing=7)
        recent = [trade(300 + i * 3, -0.4) for i in range(6)]
        r = shadow.evaluate_monitors(old + recent, asof=BASE + 320 * DAY)[1]
        assert r.state == "ALERT"

    def test_ok_when_the_window_is_healthy(self):
        r = shadow.evaluate_monitors(series([0.3] * 8), asof=BASE + 60 * DAY)[1]
        assert r.state == "OK"


class TestM3Concentration:

    def test_insufficient_below_ten_trades(self):
        r = shadow.evaluate_monitors(series([0.2] * 9))[2]
        assert r.state == "INSUFFICIENT_DATA"

    def test_evenly_spread_returns_are_ok(self):
        r = shadow.evaluate_monitors(series([0.2] * 20))[2]
        assert r.state == "OK" and r.value == pytest.approx(3 / 20)

    def test_a_few_trades_carrying_everything_warns(self):
        trades = series([3.0, 3.0, 3.0] + [0.05] * 17)
        r = shadow.evaluate_monitors(trades)[2]
        assert r.state == "WARN" and r.value > 0.60

    def test_a_non_positive_total_reports_no_number(self):
        """A share of a non-positive total is not a number to act on, and
        reporting one would be worse than reporting nothing."""
        assert shadow.concentration_top3(series([-0.5] * 12)) is None
        r = shadow.evaluate_monitors(series([-0.5] * 12))[2]
        assert r.state == "INSUFFICIENT_DATA"


class TestM4Halves:

    def test_insufficient_below_twenty(self):
        r = shadow.evaluate_monitors(series([0.2] * 19))[3]
        assert r.state == "INSUFFICIENT_DATA"

    def test_the_slice57_shape_warns(self):
        """Earlier half healthy, recent half negative — the decay this
        programme has already seen once, at OOS Q1 +0.3164 / Q2 +0.0236."""
        r = shadow.evaluate_monitors(series([0.3] * 10 + [-0.1] * 10))[3]
        assert r.state == "WARN"

    def test_a_flat_but_positive_recent_half_does_not_warn(self):
        """Q2 at +0.0236 is flat, not negative. The monitor fires on the sign,
        which is what §41e declared — not on 'weaker than before', which would
        be a judgement nobody wrote down in advance."""
        r = shadow.evaluate_monitors(series([0.3] * 10 + [0.0236] * 10))[3]
        assert r.state == "OK"


class TestTheAggregateStatus:

    def test_worst_state_wins(self):
        readings = shadow.evaluate_monitors(series([-0.3] * 25))
        assert shadow.monitor_status(readings) == "ALERT"

    def test_alert_blocks_entries_and_warn_does_not(self):
        assert shadow.entries_blocked_by_monitor(
            shadow.evaluate_monitors(series([-0.3] * 12)))
        assert not shadow.entries_blocked_by_monitor(
            shadow.evaluate_monitors(series([-0.1] * 12)))

    def test_no_data_does_not_block(self):
        assert not shadow.entries_blocked_by_monitor(
            shadow.evaluate_monitors([]))


class TestMonitorsDoNotTrade:
    """§41e: a monitor that could retune the signal would be a fitting
    procedure with a safety label on it."""

    FORBIDDEN = ("FUND_ABS", "STOP_ATR", "TAKE_PROFIT_R", "HORIZON",
                 "clear_kill_switch", "revoke_cleared_edge", "place_order",
                 "submit_order", "resize")

    def test_the_monitor_functions_touch_nothing(self):
        """AST, not a text search.

        The first version grepped the source and went red on
        `entries_blocked_by_monitor`'s own docstring, which says the function
        "does not resize, reverse, retune a constant...". EDGE.md §12c for the
        sixth time: a guard keyed on a string forbids the sentence written to
        satisfy it. What must be absent is the NAME BEING USED — a reference or
        a call — not the word appearing in prose that explains its absence.
        """
        for name in ("rolling_mean_r", "rolling_mean_r_by_days",
                     "concentration_top3", "halves_split",
                     "evaluate_monitors", "monitor_status",
                     "entries_blocked_by_monitor"):
            tree = ast.parse(inspect.getsource(getattr(shadow, name)))
            used = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Name):
                    used.add(node.id)
                elif isinstance(node, ast.Attribute):
                    used.add(node.attr)
            for token in self.FORBIDDEN:
                assert token not in used, (name, token)

    def test_that_guard_would_catch_a_real_use(self):
        """The control: an AST ban and a text ban differ, and this shows how."""
        tree = ast.parse("def f(x):\n    return resize(x)\n")
        used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        assert "resize" in used

    def test_a_breach_does_not_revoke_the_research_clear(self):
        """A bad shadow week is not a withdrawal of a research claim."""
        readings = shadow.evaluate_monitors(series([-1.0] * 30))
        assert shadow.monitor_status(readings) == "ALERT"
        assert not shadow.is_revoked(shadow.SHADOW_SIGNAL)
        assert ps.current().cleared_edge_signal == shadow.SHADOW_SIGNAL


# ===========================================================================
# 5 — revocation is human-only, and provably so
# ===========================================================================


class TestRevokeIsHumanOnly:

    def test_the_acknowledgement_is_a_literal(self):
        assert shadow.REVOCATION_ACK == "HUMAN_REVOKED_CLEARED_EDGE"

    def test_it_is_not_derivable_from_config(self):
        """The kill switch's clear token has the same property, for the same
        reason: a token a process could assemble from settings is a token it
        can supply to itself."""
        import config as _config
        blob = json.dumps(
            {k: str(v) for k, v in vars(_config.load({})).items()},
            default=str)
        assert shadow.REVOCATION_ACK not in blob

    @pytest.mark.parametrize("ack", ["", "yes", "HUMAN_CLEARED_KILL_SWITCH",
                                     "human_revoked_cleared_edge"])
    def test_a_wrong_acknowledgement_is_refused(self, ack, tmp_path):
        with pytest.raises(PermissionError):
            shadow.revoke_cleared_edge(
                shadow.SHADOW_SIGNAL, acknowledgement=ack, reason="x",
                operator="y", at_utc="2026-01-01T00:00:00Z",
                path=str(tmp_path / "r.json"))

    def test_a_revocation_must_carry_a_reason_and_an_operator(self, tmp_path):
        for kwargs in ({"reason": "  ", "operator": "y"},
                       {"reason": "x", "operator": ""}):
            with pytest.raises(ValueError):
                shadow.revoke_cleared_edge(
                    shadow.SHADOW_SIGNAL,
                    acknowledgement=shadow.REVOCATION_ACK,
                    at_utc="2026-01-01T00:00:00Z",
                    path=str(tmp_path / "r.json"), **kwargs)

    def test_a_correct_revocation_is_recorded_and_takes_effect(self, tmp_path):
        path = str(tmp_path / "r.json")
        assert not shadow.is_revoked(shadow.SHADOW_SIGNAL, path=path)
        record = shadow.revoke_cleared_edge(
            shadow.SHADOW_SIGNAL, acknowledgement=shadow.REVOCATION_ACK,
            reason="rolling mean below the alert bar for six weeks",
            operator="risk officer", at_utc="2026-03-01T00:00:00Z", path=path)
        assert record["operator"] == "risk officer"
        assert shadow.is_revoked(shadow.SHADOW_SIGNAL, path=path)

    def test_it_only_revokes_the_named_signal(self, tmp_path):
        path = str(tmp_path / "r.json")
        shadow.revoke_cleared_edge(
            "some_other_signal", acknowledgement=shadow.REVOCATION_ACK,
            reason="x", operator="y", at_utc="2026-01-01T00:00:00Z", path=path)
        assert not shadow.is_revoked(shadow.SHADOW_SIGNAL, path=path)

    def test_a_record_without_the_token_does_not_revoke(self, tmp_path):
        """Someone editing the file by hand, without knowing the token, does
        not silently withdraw a research claim."""
        path = tmp_path / "r.json"
        path.write_text(json.dumps({"revocations": [
            {"signal": shadow.SHADOW_SIGNAL, "acknowledgement": "sure"}]}),
            encoding="utf-8")
        assert not shadow.is_revoked(shadow.SHADOW_SIGNAL, path=str(path))

    def test_a_corrupt_file_revokes_everything(self, tmp_path):
        """'Cannot tell' and 'not revoked' must not be the same answer."""
        path = tmp_path / "r.json"
        path.write_text("{ not json", encoding="utf-8")
        assert shadow.is_revoked(shadow.SHADOW_SIGNAL, path=str(path))

    def test_no_trading_or_model_module_can_call_the_revoke_path(self):
        """A model that could revoke an edge is a model with an opinion about
        research."""
        for module in ("risk_management", "trading_engine", "policy",
                       "position_sizing", "main", "config", "ml_strategy",
                       "shadow_strategy", "technical_analysis"):
            source = inspect.getsource(__import__(module))
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    func = node.func
                    called = (func.attr if isinstance(func, ast.Attribute)
                              else func.id if isinstance(func, ast.Name)
                              else "")
                    assert called != "revoke_cleared_edge", module

    def test_a_revoked_signal_cannot_register(self, tmp_path, monkeypatch):
        """The whole point: revocation reaches the registration hook."""
        path = str(tmp_path / "r.json")
        shadow.revoke_cleared_edge(
            shadow.SHADOW_SIGNAL, acknowledgement=shadow.REVOCATION_ACK,
            reason="decayed", operator="human",
            at_utc="2026-03-01T00:00:00Z", path=path)
        monkeypatch.setattr(shadow, "REVOCATIONS_PATH", path)
        assert ps.cleared_edge_signal_from_artifacts(ARTIFACTS) is None

    def test_registration_returns_once_the_revocation_is_lifted(self):
        """The control: without it, 'refuses' could mean 'is broken'."""
        assert ps.cleared_edge_signal_from_artifacts(ARTIFACTS) == \
            shadow.SHADOW_SIGNAL

    def test_registering_is_hard_and_revoking_is_easy_on_purpose(self):
        """§41f's asymmetry: registration needs a pre-declared gate,
        revocation needs only a human saying so. It is easy to stop claiming
        something and hard to start."""
        assert shadow.SHADOW_SIGNAL in ps.OOS_GATED_REGISTRATION
        signature = inspect.signature(shadow.revoke_cleared_edge)
        assert "acknowledgement" in signature.parameters
        assert "gate" not in signature.parameters


# ===========================================================================
# 6 — the strategy: what it proposes and what it refuses
# ===========================================================================


class _Bar:
    def __init__(self, ms, o, h, l, c):
        self.start_ms = ms
        self.open, self.high, self.low, self.close = o, h, l, c
        self.volume = 1.0


def _bars(n=40, price=50_000.0):
    day = 86_400_000
    epoch = int(dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc).timestamp()
                * 1000)
    return [_Bar(epoch + i * day, price, price * 1.01, price * 0.99, price)
            for i in range(n)]


def _rich_funding(bars):
    """Rich funding at the LAST decision bar only, so it is a RUN START.

    Updated when the one-trade-per-run rule was added: a fixture that made
    every bar rich left the last decision bar mid-run, and the strategy
    correctly proposed nothing. The fixture was the thing that was wrong, and
    `TestOneTradePerRun` below covers the mid-run case explicitly.
    """
    decision = len(bars) - 2
    rates = [0.00001] * len(bars)
    rates[decision] = 0.0005
    return fb.FundingSeries(
        [int(b.start_ms) + 3_600_000 for b in bars], rates, symbol="BTCUSDT")


def _strategy(**over):
    bars = over.pop("bars", None) or _bars()
    return shadow_strategy.ShadowStrategy(
        status_provider=over.pop("status", lambda: Status()),
        funding_provider=lambda s: _rich_funding(bars),
        bar_provider=lambda s: bars,
        now_utc=over.pop("now", lambda: dt.datetime(2026, 3, 1,
                                                    tzinfo=dt.timezone.utc)),
        **over)


class TestTheShadowStrategy:

    def test_it_proposes_a_capped_short_on_rich_funding(self):
        intent = _strategy().signal_for("BTCUSDT")
        assert intent is not None
        assert intent.symbol == "BTCUSDT"
        assert intent.side == "Sell"          # rich funding -> fade -> short
        assert intent.stop_price > intent.entry_price
        assert "SHADOW" in intent.reason
        assert "Not a live order" in intent.reason

    def test_it_never_sets_win_probability(self):
        """That field belongs to a promoted model, and no model exists."""
        assert _strategy().signal_for("BTCUSDT").win_probability is None

    def test_the_geometry_is_the_frozen_geometry(self):
        intent = _strategy().signal_for("BTCUSDT")
        stop_distance = abs(intent.stop_price - intent.entry_price)
        target = intent.take_profits[0][0]
        reward = abs(target - intent.entry_price)
        assert reward == pytest.approx(fb.TAKE_PROFIT_R * stop_distance)

    def test_it_refuses_another_symbol(self):
        assert _strategy().signal_for("ETHUSDT") is None

    def test_it_refuses_when_the_flag_is_null(self):
        s = _strategy(status=lambda: Status(cleared=None))
        assert s.signal_for("BTCUSDT") is None
        assert "cleared_edge_signal" in s.last_refusal

    def test_it_refuses_when_a_position_is_open(self):
        s = _strategy()
        s.record_fill(day="2026-03-01")
        assert s.signal_for("BTCUSDT") is None
        assert "already open" in s.last_refusal

    def test_it_refuses_a_second_entry_the_same_day(self):
        s = _strategy()
        s.record_fill(day="2026-03-01")
        s.record_close(trade(0, 0.1))
        assert s.signal_for("BTCUSDT") is None
        assert "per calendar day" in s.last_refusal

    def test_it_refuses_while_a_monitor_alerts(self):
        s = _strategy(closed_trades=series([-0.4] * 12))
        assert s.signal_for("BTCUSDT") is None
        assert "ALERT" in s.last_refusal

    def test_the_alert_refusal_says_the_signal_is_not_retuned(self):
        s = _strategy(closed_trades=series([-0.4] * 12))
        s.signal_for("BTCUSDT")
        assert "NOT retuned" in s.last_refusal
        assert "research clear is NOT touched" in s.last_refusal

    def test_a_warn_does_not_block(self):
        s = _strategy(closed_trades=series([-0.1] * 12))
        assert s.signal_for("BTCUSDT") is not None

    def test_no_setup_means_no_intent(self):
        bars = _bars()
        flat = fb.FundingSeries([int(b.start_ms) + 3_600_000 for b in bars],
                                [0.00001] * len(bars), symbol="BTCUSDT")
        s = shadow_strategy.ShadowStrategy(
            status_provider=lambda: Status(),
            funding_provider=lambda _s: flat,
            bar_provider=lambda _s: bars,
            now_utc=lambda: dt.datetime(2026, 3, 1, tzinfo=dt.timezone.utc))
        assert s.signal_for("BTCUSDT") is None

    def test_it_never_raises_into_the_loop(self):
        def explode(_s):
            raise RuntimeError("exchange down")
        s = shadow_strategy.ShadowStrategy(
            status_provider=lambda: Status(),
            funding_provider=explode, bar_provider=explode,
            now_utc=lambda: dt.datetime(2026, 3, 1, tzinfo=dt.timezone.utc))
        assert s.signal_for("BTCUSDT") is None

    def test_the_atr_agrees_with_the_measurement_tools(self):
        """The miniature ATR here must equal the one every EDGE.md number was
        computed with, or the shadow prices a different stop from the cleared
        rule."""
        import numpy as np
        import sweep_geometry as sweep
        bars = _bars(60)
        for i, b in enumerate(bars):
            b.high = 50_000 + i * 37.0
            b.low = 49_000 + i * 11.0
            b.close = 49_500 + i * 23.0
        mine = shadow_strategy._wilder_atr_last(bars, 14)
        theirs = sweep.wilder_atr(
            np.array([b.high for b in bars]), np.array([b.low for b in bars]),
            np.array([b.close for b in bars]), 14)[-1]
        assert mine == pytest.approx(float(theirs), rel=1e-9)

    def test_the_snapshot_carries_no_pnl_field(self):
        snapshot = _strategy(closed_trades=series([0.2] * 12)).snapshot()
        blob = json.dumps(snapshot).lower()
        for marker in ps.FORBIDDEN_FIELD_MARKERS:
            assert marker not in blob, marker

    def test_the_snapshot_reports_the_caps_and_the_monitors(self):
        snapshot = _strategy(closed_trades=series([0.2] * 12)).snapshot()
        assert snapshot["caps"]["max_notional_usd"] == 100.0
        assert len(snapshot["monitors"]) == 4
        assert snapshot["monitor_status"] in {"OK", "WARN", "ALERT",
                                              "INSUFFICIENT_DATA"}


# ===========================================================================
# 7 — nothing was unblocked
# ===========================================================================


class TestNothingWasArmed:

    def test_live_is_still_blocked(self):
        status = ps.current()
        assert status.live_authorized is False
        assert status.execution_mode == "paper"
        assert status.policy_mode == "off"

    def test_no_model_exists_and_no_promote_path_was_added(self):
        """AST for the identifiers, text only for the path literal.

        The first version banned the substring "train" and went red on the word
        "Constrained" in `shadow`'s own opening line. §12c for the seventh
        time. Identifiers are checked as identifiers; the one thing a text
        search is right for is a literal path, which cannot appear by accident
        inside an English word.
        """
        assert not os.path.exists(os.path.join(REPO, "models", "current"))
        for module in ("shadow", "shadow_strategy"):
            source = inspect.getsource(__import__(module))
            assert "models/current" not in source, module
            tree = ast.parse(source)
            used = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Name):
                    used.add(node.id)
                elif isinstance(node, ast.Attribute):
                    used.add(node.attr)
                elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                    used.add(node.name)
            for token in ("promote", "train", "fit", "load_model"):
                assert token not in used, (module, token)

    def test_the_shadow_modules_cannot_clear_the_kill_switch(self):
        """Covered by the call-level check above; kept as its own named test
        because this is the single most important thing the shadow layer must
        not be able to do."""
        for module in ("shadow", "shadow_strategy"):
            tree = ast.parse(inspect.getsource(__import__(module)))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    func = node.func
                    called = (func.attr if isinstance(func, ast.Attribute)
                              else func.id if isinstance(func, ast.Name)
                              else "")
                    assert "kill_switch" not in called, module

    def test_the_shadow_modules_place_no_orders(self):
        """AST again — the docstrings explain that they place none."""
        for module in ("shadow", "shadow_strategy"):
            tree = ast.parse(inspect.getsource(__import__(module)))
            called = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    func = node.func
                    called.add(func.attr if isinstance(func, ast.Attribute)
                               else func.id if isinstance(func, ast.Name)
                               else "")
            for token in ("place_order", "submit_order", "create_order",
                          "clear_kill_switch"):
                assert token not in called, (module, token)

    def test_the_eleven_freezes_are_intact(self):
        assert len(ps.ABSENT_SIGNALS) == 11
        assert "funding_carry_fade_v1" in ps.ABSENT_SIGNALS
        assert ps.FROZEN_STATUS["funding_carry_fade_v1"] == "ABSENT"

    def test_a_forged_positive_for_a_frozen_name_is_still_refused(self,
                                                                  tmp_path):
        summary = {
            "symbol": "BTCUSD", "signal": "donchian_breakout_v1",
            "observed": {"n_trades": 200, "mean_r": 0.9},
            "m1": {"percentile": 99.9, "bar": 95.0, "passed": True},
            "m2": {"percentile": 99.9, "bar": 95.0, "passed": True},
            "control_validated": True, "verdict": "EDGE_EVIDENCE_POSITIVE",
        }
        (tmp_path / "x_summary.json").write_text(json.dumps(summary),
                                                 encoding="utf-8")
        assert ps.cleared_edge_signal_from_artifacts(str(tmp_path)) is None

    def test_the_oos_artefacts_were_not_re_scored(self):
        with open(os.path.join(
                ARTIFACTS, "slice57_oos_edge_BTCUSDT_summary.json"),
                encoding="utf-8") as handle:
            d = json.load(handle)
        assert d["observed"]["n_trades"] == 41
        assert round(d["m1"]["percentile"], 2) == 95.13
        assert d["m2"]["percentile"] == 96.0
        assert d["observed"]["mean_r"] == pytest.approx(0.1735699020942922)
        assert d["oos_folds_sha256"] == fb.folds_sha256()


# ===========================================================================
# 8 — the session surface
# ===========================================================================


class TestTheSessionSurface:

    def test_the_research_clear_block_is_present_and_honest(self):
        block = sl._research_clear()
        assert block["signal"] == shadow.SHADOW_SIGNAL
        assert block["stage"] == "research_clear"
        assert block["live_claim"] is False
        assert block["window"] == "oos_late"
        assert block["scored_trades"] == 41
        assert block["m1"] == 95.13

    def test_it_states_the_thinness_rather_than_the_name_alone(self):
        """A bare name invites a reader to assume more behind it."""
        block = sl._research_clear()
        assert "not evidence of skill" in block["note"]
        assert "nothing is armed" in block["note"]

    def test_it_carries_no_pnl_shaped_field(self):
        blob = json.dumps(sl._research_clear()).lower()
        for marker in sl.FORBIDDEN_FIELD_MARKERS:
            assert marker not in blob, marker

    def test_the_no_edge_claim_line_is_unchanged(self):
        assert "NO EDGE CLAIM" in sl.NO_EDGE_CLAIM
        assert ps.current().no_edge_claim == sl.NO_EDGE_CLAIM

    def test_the_block_is_none_when_nothing_is_cleared(self, monkeypatch):
        monkeypatch.setattr(
            ps, "cleared_edge_signal_from_artifacts", lambda *a, **k: None)
        assert sl._research_clear() is None


# ===========================================================================
# 9 — the shadow pilots the CLEARED rule's schedule, not a cousin of it
# ===========================================================================


class TestOneTradePerRun:
    """The defect the STEP-4 replay caught, and the guard against it returning.

    Rich funding persists for days. The cleared rule enters ONCE per contiguous
    run of setups — slice 17's rule, and the rule the Stage-1 measurement was
    scored under. The first version of this shadow layer entered on every
    flagged bar, which on the out-of-sample window took 55 trades instead of 41
    and produced a NEGATIVE mean net R where the rule it was supposed to be
    shadowing scored +0.1736.

    A monitor watching the wrong object is worse than no monitor, because it
    looks like diligence. These tests pin the schedule.
    """

    def _bars_and_funding(self, rich_days):
        """`rich_days` is a set of bar indices where |f| clears the threshold."""
        bars = _bars(40)
        times = [int(b.start_ms) + 3_600_000 for b in bars]
        rates = [0.0005 if i in rich_days else 0.00001
                 for i in range(len(bars))]
        return bars, fb.FundingSeries(times, rates, symbol="BTCUSDT")

    def _strategy_at(self, bars, funding):
        return shadow_strategy.ShadowStrategy(
            status_provider=lambda: Status(),
            funding_provider=lambda _s: funding,
            bar_provider=lambda _s: bars,
            now_utc=lambda: dt.datetime(2026, 3, 1, tzinfo=dt.timezone.utc))

    def test_the_first_bar_of_a_run_proposes(self):
        bars, funding = self._bars_and_funding({38})     # decision bar = 38
        assert self._strategy_at(bars, funding).signal_for("BTCUSDT") is not None

    def test_a_mid_run_bar_does_not_propose(self):
        """The whole defect, in one assertion."""
        bars, funding = self._bars_and_funding({37, 38})
        s = self._strategy_at(bars, funding)
        assert s.signal_for("BTCUSDT") is None
        assert "no funding setup" in s.last_refusal

    def test_a_long_run_proposes_only_at_its_start(self):
        bars, funding = self._bars_and_funding(set(range(30, 39)))
        assert self._strategy_at(bars, funding).signal_for("BTCUSDT") is None

    def test_a_gap_starts_a_new_run(self):
        """setup, gap, setup -> the second one is a run start and proposes."""
        bars, funding = self._bars_and_funding({36, 38})
        assert self._strategy_at(bars, funding).signal_for("BTCUSDT") is not None


class TestTheShadowReplayArtefact:
    """The STEP-4 wiring proof, and the labels that keep it from being read as
    evidence."""

    PATH = os.path.join(ARTIFACTS, "slice58_shadow_replay.json")

    def _payload(self):
        with open(self.PATH, encoding="utf-8") as handle:
            return json.load(handle)

    def test_it_exists_and_is_labelled_as_not_evidence(self):
        payload = self._payload()
        assert payload["kind"] == "shadow_replay"
        assert payload["is_stage1_evidence"] is False
        assert payload["registration_eligible"] is False
        assert "no rotation null" in payload["why_not_evidence"].lower()

    def test_it_is_not_a_summary_file_and_cannot_register(self):
        """The registration hook only reads `*_summary.json`. This is not one,
        which is a structural refusal rather than a policy one."""
        assert not self.PATH.endswith("_summary.json")
        assert payload_registers_nothing(self._payload())

    def test_the_replay_used_the_cleared_rules_schedule(self):
        """41 candidates — the same number the Stage-1 run scheduled."""
        payload = self._payload()
        assert payload["setups_considered"] == 41
        assert payload["shadow_trades"] <= payload["setups_considered"]
        assert payload["entries_refused_by_caps"] == \
            payload["setups_considered"] - payload["shadow_trades"]

    def test_it_records_the_defect_that_produced_the_wrong_schedule(self):
        note = self._payload()["capping_note"]
        assert "55 trades" in note
        assert "mid-run" in note

    def test_the_constants_fingerprint_is_the_frozen_one(self):
        assert self._payload()["constants_fingerprint"] == \
            shadow.CONSTANTS_FINGERPRINT

    def test_the_replay_did_not_change_the_cleared_flag(self):
        payload = self._payload()
        assert payload["cleared_edge_signal_after_replay"] == \
            shadow.SHADOW_SIGNAL
        assert payload["live_authorized"] is False
        assert payload["models_current_present"] is False

    def test_the_predeclared_halves_monitor_fires_on_the_real_window(self):
        """Recorded as a FINDING, not smoothed away.

        M-4 was declared in EDGE.md §41e before any replay ran, and on the real
        out-of-sample window it WARNs: the earlier half of the capped pilot is
        strongly positive and the recent half is negative. That is the decay
        §41b said to expect, showing up in the apparatus built to detect it.

        It does not block entries — WARN never does — and it changes no
        constant and no research flag. This test exists so that a later slice
        cannot quietly make it stop firing.
        """
        payload = self._payload()
        m4 = next(m for m in payload["monitors"] if m["name"] == "M4_halves")
        assert m4["state"] == "WARN"
        assert payload["monitor_status"] == "WARN"
        assert payload["entries_blocked_by_monitor"] is False


def payload_registers_nothing(payload):
    """A shadow replay carries none of the fields the OOS gate requires."""
    return not all(key in payload for key in
                   ("verdict", "m1", "m2", "observed", "oos_folds_sha256"))
