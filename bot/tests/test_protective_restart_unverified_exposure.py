"""An open position that is only half read is never a NORMAL restart.

`assess_restart` states the invariant ("Unknown exposure is UNKNOWN, never
flat ... entries are allowed only in NORMAL") but derived `degraded` from the
database, the order log and the kill switch only. A venue whose perp short
read fine and whose spot balance did not (or the reverse) left every one of
those clear, so the verdict was NORMAL with ``entries_allowed`` True and no
operator alert, while ``position_unknown`` was True. The existing test of this
shape (`test_one_leg_open_other_unreadable_still_exits_known_leg`) always
supplied an environment conflict, which hid it.
"""
import pytest

from protective_restart import (NORMAL, PROTECT_ONLY, Exposure, VenueSnapshot,
                                assess_restart)


def verdict(venue):
    return assess_restart(
        environment_conflict=None, store_readable=True, orders=[], venue=venue,
        kill_switch_engaged=False, entries_enabled=True)


@pytest.mark.parametrize("venue", [
    VenueSnapshot(perp_qty=-0.5, spot_qty=None),
    VenueSnapshot(perp_qty=None, spot_qty=0.5),
    VenueSnapshot(perp_qty=0.0, spot_qty=float("nan")),
    VenueSnapshot(perp_qty=-0.5, spot_qty=float("inf")),
])
def test_a_half_read_open_position_is_protect_only_with_an_alert(venue):
    v = verdict(venue)
    assert v.exposure is Exposure.OPEN or v.exposure is Exposure.UNKNOWN
    assert v.position_unknown and not v.position_verified
    assert v.mode != NORMAL
    assert not v.entries_allowed
    assert v.required_operator_alert


def test_the_readable_leg_is_still_the_only_exit_leg():
    v = verdict(VenueSnapshot(perp_qty=-0.5, spot_qty=None))
    assert v.mode == PROTECT_ONLY
    assert [leg.leg for leg in v.exit_legs] == ["perp"]


def test_a_fully_read_position_is_still_normal():
    v = verdict(VenueSnapshot(perp_qty=-0.5, spot_qty=0.5))
    assert v.mode == NORMAL and v.entries_allowed
    assert not v.required_operator_alert
