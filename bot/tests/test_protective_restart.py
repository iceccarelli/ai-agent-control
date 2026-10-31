import json

import pytest

import protective_restart as pr
from protective_restart import (CANNOT_PROTECT, NORMAL, PROTECT_ONLY, Exposure,
                                OrderFact, VenueSnapshot, assess_restart)

SHORT = VenueSnapshot(perp_qty=-0.5, spot_qty=0.5)
FLAT = VenueSnapshot(perp_qty=0.0, spot_qty=0.0)


def run(**kw):
    base = dict(environment_conflict=None, store_readable=True, orders=[],
                venue=SHORT, kill_switch_engaged=False, entries_enabled=True)
    base.update(kw)
    return assess_restart(**base)


def test_clean_restart_is_normal():
    v = run()
    assert v.mode == NORMAL and v.entries_allowed and v.exits_allowed
    assert not v.required_operator_alert and v.position_verified


def test_held_short_with_spot_hedge_environment_conflict():
    v = run(environment_conflict="db recorded ['testnet'], process mainnet")
    assert v.mode == PROTECT_ONLY
    assert not v.entries_allowed and v.exits_allowed
    assert v.position_verified and not v.position_unknown
    assert v.required_operator_alert
    legs = {l.leg: l for l in v.exit_legs}
    assert legs["perp"].side == "Buy" and legs["perp"].qty == 0.5
    assert legs["spot"].side == "Sell" and all(l.reduce_only for l in v.exit_legs)


def test_open_spot_hedge_only_leg_is_protected():
    v = run(venue=VenueSnapshot(0.0, 1.0), environment_conflict="x")
    assert v.mode == PROTECT_ONLY and v.exposure is Exposure.OPEN
    assert [l.leg for l in v.exit_legs] == ["spot"]


def test_partially_filled_order_is_not_resent_and_needs_reconciliation():
    o = OrderFact("L1", "partial", "linear", 1.0, 0.4)
    v = run(orders=[o])
    assert v.mode == PROTECT_ONLY and not v.entries_allowed
    assert "L1" in v.no_resend_links and v.reconciliation_required
    assert pr.may_send_order(v, order_link_id="L1", reduce_only=True)[0] is False
    assert pr.may_send_order(v, order_link_id="NEW", reduce_only=True)[0] is True
    assert pr.may_send_order(v, order_link_id="NEW", reduce_only=False)[0] is False


@pytest.mark.parametrize("st", ["unknown", "timed_out", "submitted",
                                "reconciliation_required", "contradicted",
                                "pending", "cancel_requested", "weird_new"])
def test_unresolved_states_never_resent(st):
    v = run(orders=[OrderFact("A", st)])
    assert "A" in v.no_resend_links and v.mode == PROTECT_ONLY
    assert v.required_operator_alert


def test_terminal_orders_are_ignored():
    v = run(orders=[OrderFact("A", "filled", filled_qty=1.0),
                    OrderFact("B", "never_sent")])
    assert v.mode == NORMAL and not v.no_resend_links


def test_unreadable_order_log_is_not_empty():
    v = run(orders=None)
    assert v.mode == PROTECT_ONLY and v.reconciliation_required


def test_unavailable_db_with_verified_position_still_protects():
    v = run(store_readable=False, orders=None)
    assert v.mode == PROTECT_ONLY and v.exits_allowed and not v.entries_allowed
    assert v.exposure is Exposure.OPEN


def test_unavailable_db_and_unreadable_venue_cannot_protect():
    v = run(store_readable=False, orders=None, venue=None)
    assert v.mode == CANNOT_PROTECT and v.position_unknown
    assert v.exposure is Exposure.UNKNOWN
    assert not v.exits_allowed and not v.entries_allowed
    assert v.alert.severity == "CRITICAL" and "HUMAN ACTION" in v.alert.message
    assert pr.may_send_order(v, order_link_id="N", reduce_only=True)[0] is False


def test_unknown_is_not_flat():
    for snap in (None, VenueSnapshot(None, None), VenueSnapshot(0.0, None),
                 VenueSnapshot(float("nan"), 0.0)):
        v = run(venue=snap)
        assert v.exposure is Exposure.UNKNOWN and v.mode == CANNOT_PROTECT
    assert pr.classify_exposure(FLAT) is Exposure.FLAT


def test_conflict_with_flat_venue_is_protect_only_not_normal():
    v = run(environment_conflict="mixed", venue=FLAT)
    assert v.mode == PROTECT_ONLY and v.exposure is Exposure.FLAT
    assert not v.entries_allowed and not v.exit_legs


def test_one_leg_open_other_unreadable_still_exits_known_leg():
    v = run(venue=VenueSnapshot(-0.5, None), environment_conflict="x")
    assert v.exposure is Exposure.OPEN and not v.position_verified
    assert v.position_unknown and v.mode == PROTECT_ONLY
    assert [l.leg for l in v.exit_legs] == ["perp"]


def test_entries_disabled_flag_blocks_entries_even_when_normal():
    v = run(entries_enabled=False)
    assert v.mode == NORMAL and not v.entries_allowed


def test_kill_switch_engaged_or_unreadable_is_protect_only():
    for ks in (True, None):
        v = run(kill_switch_engaged=ks)
        assert v.mode == PROTECT_ONLY and not v.entries_allowed
        assert v.exits_allowed


def test_exit_path_refused_with_open_position_cannot_protect():
    v = run(environment_conflict="x", exits_permitted=False,
            exits_refused_reason="mainnet not armed")
    assert v.mode == CANNOT_PROTECT and v.alert.required
    assert not v.exits_allowed


def test_position_requiring_immediate_reduction():
    v = run(environment_conflict="x", immediate_reduction_required=True)
    assert v.reduce_now and v.exits_allowed and v.alert.severity == "CRITICAL"
    v2 = run(immediate_reduction_required=True, exits_permitted=False)
    assert v2.mode == CANNOT_PROTECT and not v2.reduce_now


def test_long_perp_exit_side_is_sell():
    v = run(venue=VenueSnapshot(2.0, 0.0), environment_conflict="x")
    assert v.exit_legs[0].side == "Sell"


def test_alert_payload_is_json_and_complete():
    v = run(environment_conflict="x", orders=[OrderFact("L", "unknown")])
    p = pr.build_alert_payload(v)
    json.dumps(p)
    assert p["event"] == "protective_restart" and p["mode"] == PROTECT_ONLY
    assert p["no_resend_links"] == ["L"] and p["operator_alert_required"]
    q = pr.build_alert_payload(run(venue=None))
    assert q["level"] == "critical" and q["exposure"] == "UNKNOWN"
    assert pr.build_alert_payload(run())["level"] == "info"


def test_verdict_is_frozen():
    with pytest.raises(Exception):
        run().mode = "X"
